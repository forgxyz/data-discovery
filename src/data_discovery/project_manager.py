"""
ProjectManager for multi-project dbt artifact resolution and model discovery.

Handles loading artifacts from local paths and GitHub repositories with caching,
supports multi-project searches, and provides unified interface for project-aware tools.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Union
import logging

import aiohttp

from .resources import resource_registry

logger = logging.getLogger(__name__)


class ProjectManagerConfig:
    """Configuration for ProjectManager."""
    
    def __init__(self):
        self.MAX_PROJECTS = int(os.getenv('MAX_PROJECTS', '5'))
        self.DEPLOYMENT_MODE = os.getenv('DEPLOYMENT_MODE', 'local').lower()
        self.CACHE_DIR = os.getenv('CACHE_DIR', self._get_default_cache_dir())
        self.CACHE_TTL_SECONDS = int(os.getenv('CACHE_TTL_SECONDS', '86400'))  # 24 hours
        self.MAX_FILE_SIZE = int(os.getenv('MAX_FILE_SIZE', '52428800'))  # 50MB
    
    def _get_default_cache_dir(self) -> str:
        """Get default cache directory based on deployment mode."""
        if self.DEPLOYMENT_MODE == 'local':
            # Local development - use relative target directory
            return 'target'
        elif self.DEPLOYMENT_MODE in ['desktop', 'remote']:
            # Claude Desktop or remote deployment - use absolute writable path
            import tempfile
            return os.path.join(os.path.expanduser('~'), '.cache', 'fsc-dbt-mcp')
        else:
            # Unknown mode - fallback to temp directory
            import tempfile
            return os.path.join(tempfile.gettempdir(), 'fsc-dbt-mcp')


class ProjectManager:
    """Manages dbt project artifacts and multi-project operations."""
    
    def __init__(self, config: Optional[ProjectManagerConfig] = None):
        self.config = config or ProjectManagerConfig()
        self._ensure_cache_directory()
    
    def _ensure_cache_directory(self):
        """Ensure cache directory exists."""
        cache_path = Path(self.config.CACHE_DIR)
        logger.info(f"ProjectManager deployment mode: {self.config.DEPLOYMENT_MODE}")
        logger.info(f"Using cache directory: {cache_path.absolute()}")
        
        try:
            cache_path.mkdir(parents=True, exist_ok=True)
            logger.info(f"Cache directory created successfully: {cache_path.absolute()}")
        except (OSError, PermissionError) as e:
            logger.error(f"Failed to create cache directory '{cache_path.absolute()}': {e}")
            logger.error(f"Deployment mode: {self.config.DEPLOYMENT_MODE}")
            logger.error("Try setting DEPLOYMENT_MODE environment variable to 'desktop' for Claude Desktop")
            raise RuntimeError(f"Cannot create cache directory. Error: {e}")
    
    def _validate_resource_ids(self, resource_ids: Union[str, List[str], None]) -> List[str]:
        """Validate and normalize resource IDs."""
        # Get available resource IDs for error messages
        available_resource_ids = resource_registry.list_project_ids()
        
        # Handle null/None case - treat as empty (search all resources)
        if resource_ids is None:
            return []
        
        if isinstance(resource_ids, str):
            resource_ids = [resource_ids]
        
        if not isinstance(resource_ids, list):
            raise ValueError(f"resource_ids must be a string or list of strings. Available resources: {available_resource_ids}")
        
        if len(resource_ids) == 0:
            # Empty list is valid - means search all resources
            return []
        
        if len(resource_ids) > self.config.MAX_PROJECTS:
            raise ValueError(f"Cannot request more than {self.config.MAX_PROJECTS} resources (requested: {len(resource_ids)}). Available resources: {available_resource_ids}")
        
        # Validate each resource ID exists in resource registry
        for resource_id in resource_ids:
            # Check for None/null values in the list
            if resource_id is None or resource_id == "null":
                raise ValueError(f"resource_id cannot be null. Available resources: {available_resource_ids}")
            
            # Check for empty strings
            if not isinstance(resource_id, str) or not resource_id.strip():
                raise ValueError(f"resource_id must be a non-empty string. Available resources: {available_resource_ids}")
            
            if resource_id not in available_resource_ids:
                raise ValueError(f"Unknown resource ID: {resource_id}. Available resources: {available_resource_ids}")
        
        return resource_ids
    
    def _extract_project_from_unique_id(self, unique_id: str) -> Optional[str]:
        """Extract project ID from unique_id format: model.project_name.model_name"""
        if not unique_id or not unique_id.startswith("model."):
            return None
        
        parts = unique_id.split('.')
        if len(parts) < 3:
            return None
        
        # Extract project name and convert to project ID format
        project_name = parts[1]
        
        # Get available project IDs and try to match
        available_project_ids = resource_registry.list_project_ids()
        
        # Try exact match first
        if project_name in available_project_ids:
            return project_name
        
        # Try with common transformations (underscore to dash, etc.)
        project_id_dash = project_name.replace('_', '-')
        if project_id_dash in available_project_ids:
            return project_id_dash
        
        project_id_underscore = project_name.replace('-', '_')
        if project_id_underscore in available_project_ids:
            return project_id_underscore
        
        return None
    
    def _validate_unique_id_project(self, unique_id: str) -> str:
        """Validate that the project in unique_id exists and return project ID."""
        project_id = self._extract_project_from_unique_id(unique_id)
        available_project_ids = resource_registry.list_project_ids()
        
        if not project_id:
            raise ValueError(f"Cannot extract valid project from unique_id '{unique_id}'. Available projects: {available_project_ids}")
        
        return project_id
    
    def _get_cache_path(self, project_id: str) -> Path:
        """Get cache directory path for a project."""
        return Path(self.config.CACHE_DIR) / project_id
    
    def _get_cache_file_path(self, project_id: str, artifact_type: str) -> Path:
        """Get cache file path for a specific artifact."""
        return self._get_cache_path(project_id) / f"{artifact_type}.json"
    
    def _get_cache_meta_path(self, project_id: str) -> Path:
        """Get cache metadata file path."""
        return self._get_cache_path(project_id) / "cache_meta.json"
    
    def _is_cache_valid(self, project_id: str) -> bool:
        """Check if cached artifacts are still valid."""
        meta_path = self._get_cache_meta_path(project_id)
        
        if not meta_path.exists():
            return False
        
        try:
            with open(meta_path, 'r') as f:
                meta = json.load(f)
            
            cached_time = datetime.fromisoformat(meta['cached_at'])
            now = datetime.now(timezone.utc)
            age_seconds = (now - cached_time).total_seconds()
            
            return age_seconds < self.config.CACHE_TTL_SECONDS
        except Exception as e:
            logger.warning(f"Error reading cache metadata for {project_id}: {e}")
            return False
    
    def _load_cached_artifacts(self, project_id: str) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """Load artifacts from cache if valid."""
        if not self._is_cache_valid(project_id):
            return None
        
        try:
            manifest_path = self._get_cache_file_path(project_id, "manifest")
            catalog_path = self._get_cache_file_path(project_id, "catalog")
            
            if not manifest_path.exists() or not catalog_path.exists():
                return None
            
            with open(manifest_path, 'r') as f:
                manifest = json.load(f)
            
            with open(catalog_path, 'r') as f:
                catalog = json.load(f)
            
            logger.info(f"Loaded cached artifacts for project {project_id}")
            return manifest, catalog
            
        except Exception as e:
            logger.warning(f"Error loading cached artifacts for {project_id}: {e}")
            return None

    def _load_cached_artifacts_fallback(self, project_id: str) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """Load artifacts from cache regardless of validity (fallback for failed GitHub requests)."""
        try:
            manifest_path = self._get_cache_file_path(project_id, "manifest")
            catalog_path = self._get_cache_file_path(project_id, "catalog")
            
            if not manifest_path.exists() or not catalog_path.exists():
                return None
            
            with open(manifest_path, 'r') as f:
                manifest = json.load(f)
            
            with open(catalog_path, 'r') as f:
                catalog = json.load(f)
            
            logger.info(f"Loaded cached artifacts (fallback) for project {project_id}")
            return manifest, catalog
            
        except Exception as e:
            logger.warning(f"Error loading cached artifacts fallback for {project_id}: {e}")
            return None
    
    def _cache_artifacts(self, project_id: str, manifest: Dict[str, Any], catalog: Dict[str, Any]):
        """Cache artifacts with UTC timestamp metadata."""
        try:
            cache_dir = self._get_cache_path(project_id)
            cache_dir.mkdir(parents=True, exist_ok=True)
            
            # Save artifacts
            manifest_path = self._get_cache_file_path(project_id, "manifest")
            catalog_path = self._get_cache_file_path(project_id, "catalog")
            
            with open(manifest_path, 'w') as f:
                json.dump(manifest, f, indent=2)
            
            with open(catalog_path, 'w') as f:
                json.dump(catalog, f, indent=2)
            
            # Save cache metadata
            meta_path = self._get_cache_meta_path(project_id)
            cache_meta = {
                "project_id": project_id,
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "ttl_seconds": self.config.CACHE_TTL_SECONDS,
                "files": {
                    "manifest": str(manifest_path),
                    "catalog": str(catalog_path)
                }
            }
            
            with open(meta_path, 'w') as f:
                json.dump(cache_meta, f, indent=2)
            
            logger.info(f"Cached artifacts for project {project_id}")
            
        except Exception as e:
            logger.error(f"Error caching artifacts for {project_id}: {e}")
    
    async def _fetch_github_artifacts(self, project_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Fetch artifacts from GitHub repository."""
        
        project_data = resource_registry.get_project_by_id(project_id)
        
        if project_data["type"] != "github":
            raise ValueError(f"Project {project_id} is not a GitHub project")
        
        manifest_url = project_data["artifact_location"]["manifest"]
        catalog_url = project_data["artifact_location"]["catalog"]
        
        async with aiohttp.ClientSession() as session:
            try:
                # Fetch manifest
                async with session.get(manifest_url) as response:
                    if response.status != 200:
                        raise RuntimeError(f"Failed to fetch manifest from {manifest_url}: {response.status}")
                    
                    content_length = response.headers.get('content-length')
                    if content_length and int(content_length) > self.config.MAX_FILE_SIZE:
                        raise ValueError(f"Manifest file too large: {content_length} bytes")
                    
                    manifest_text = await response.text()
                    manifest = json.loads(manifest_text)
                
                # Fetch catalog
                async with session.get(catalog_url) as response:
                    if response.status != 200:
                        raise RuntimeError(f"Failed to fetch catalog from {catalog_url}: {response.status}")
                    
                    content_length = response.headers.get('content-length')
                    if content_length and int(content_length) > self.config.MAX_FILE_SIZE:
                        raise ValueError(f"Catalog file too large: {content_length} bytes")
                    
                    catalog_text = await response.text()
                    catalog = json.loads(catalog_text)
                
                logger.info(f"Successfully fetched GitHub artifacts for project {project_id}")
                return manifest, catalog
                
            except Exception as e:
                logger.error(f"Error fetching GitHub artifacts for {project_id}: {e}")
                
                # Try to load from cache as fallback
                logger.info(f"Attempting to load cached artifacts as fallback for {project_id}")
                cached_artifacts = self._load_cached_artifacts_fallback(project_id)
                
                if cached_artifacts:
                    logger.warning(f"Using cached artifacts as fallback for {project_id} due to GitHub fetch failure")
                    return cached_artifacts
                else:
                    logger.error(f"No cached artifacts available for fallback for {project_id}")
                    raise
    
    def _load_local_artifacts(self, project_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Load artifacts from local filesystem."""
        project_data = resource_registry.get_project_by_id(project_id)
        
        if project_data["type"] != "local":
            raise ValueError(f"Project {project_id} is not a local project")
        
        manifest_path = Path(project_data["artifact_location"]["manifest"])
        catalog_path = Path(project_data["artifact_location"]["catalog"])
        
        if not manifest_path.exists():
            raise FileNotFoundError(f"Manifest not found: {manifest_path}")
        
        if not catalog_path.exists():
            raise FileNotFoundError(f"Catalog not found: {catalog_path}")
        
        try:
            with open(manifest_path, 'r') as f:
                manifest = json.load(f)
            
            with open(catalog_path, 'r') as f:
                catalog = json.load(f)
            
            logger.info(f"Successfully loaded local artifacts for project {project_id}")
            return manifest, catalog
            
        except Exception as e:
            logger.error(f"Error loading local artifacts for {project_id}: {e}")
            raise
    
    async def get_project_artifacts(self, resource_ids: Union[str, List[str]]) -> Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]]:
        """Get manifest and catalog artifacts for specified resources."""
        # Validate resource IDs first - this will raise on invalid input
        # Do NOT continue processing if validation fails
        resource_ids = self._validate_resource_ids(resource_ids)
        
        # If no resource IDs specified (empty list), load all available resources
        if not resource_ids:
            resource_ids = resource_registry.list_project_ids()
            logger.info(f"No resource_ids specified, loading all available resources: {resource_ids}")
        
        artifacts = {}
        
        for project_id in resource_ids:
            try:
                # Try cache first
                cached_artifacts = self._load_cached_artifacts(project_id)
                if cached_artifacts:
                    artifacts[project_id] = cached_artifacts
                    continue
                
                # Load from source
                project_data = resource_registry.get_project_by_id(project_id)
                
                if project_data["type"] == "local":
                    manifest, catalog = self._load_local_artifacts(project_id)
                elif project_data["type"] == "github":
                    manifest, catalog = await self._fetch_github_artifacts(project_id)
                else:
                    raise ValueError(f"Unknown project type: {project_data['type']}")
                
                # Cache the artifacts
                self._cache_artifacts(project_id, manifest, catalog)
                artifacts[project_id] = (manifest, catalog)
                
            except Exception as e:
                logger.error(f"Failed to load artifacts for project {project_id}: {e}")
                # Continue with other projects instead of failing completely
                continue
        
        if not artifacts:
            available_projects = resource_registry.list_project_ids()
            if not resource_ids:
                raise RuntimeError(f"Failed to load artifacts for any available resources: {available_projects}")
            else:
                raise RuntimeError(f"Failed to load artifacts for any of the requested resources: {resource_ids}. Available resources: {available_projects}")
        
        return artifacts
    
    async def find_model_in_projects(self, model_name: str, resource_ids: Optional[Union[str, List[str]]] = None) -> List[Dict[str, Any]]:
        """Find model across specified resources or all resources."""
        if resource_ids is None:
            all_resources = resource_registry.list_project_ids()
            if len(all_resources) > self.config.MAX_PROJECTS:
                raise ValueError(f"Too many resources available ({len(all_resources)}). Please specify resource_id to search specific projects. Available resources: {all_resources[:10]}{'...' if len(all_resources) > 10 else ''}")
            resource_ids = all_resources
            logger.info(f"No resource_ids specified for model search, using all available resources: {resource_ids}")
        
        resource_ids = self._validate_resource_ids(resource_ids)
        artifacts = await self.get_project_artifacts(resource_ids)
        
        found_models = []
        
        for project_id, (manifest, catalog) in artifacts.items():
            nodes = manifest.get("nodes", {})
            
            for node_id, node in nodes.items():
                if (isinstance(node, dict) and 
                    node.get("resource_type") == "model" and 
                    node.get("name") == model_name):
                    
                    # Get catalog information if available
                    catalog_node = catalog.get("nodes", {}).get(node_id, {})
                    
                    model_info = {
                        "resource_id": project_id,
                        "unique_id": node_id,
                        "manifest_data": node,
                        "catalog_data": catalog_node
                    }
                    found_models.append(model_info)
        
        return found_models


# Global project manager instance
project_manager = ProjectManager()

__all__ = [
    "ProjectManager",
    "ProjectManagerConfig", 
    "project_manager"
]