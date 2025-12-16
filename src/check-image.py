import warnings
# Suppress urllib3 OpenSSL warning on macOS with LibreSSL before importing requests
warnings.filterwarnings('ignore', message='urllib3 v2 only supports OpenSSL')

import requests
import sys
import os
import argparse
from typing import List, Dict, Tuple, Optional

# Target architectures to check
TARGET_ARCHITECTURES = {'amd64', 'arm64'}
TIMEOUT_SECONDS = 10

def detect_registry(image: str) -> str:
    """Detect the registry type from the image name."""
    if image.startswith('ghcr.io/'):
        return 'ghcr'
    elif image.startswith('quay.io/'):
        return 'quay'
    elif image.startswith('docker.io/'):
        return 'dockerhub'
    elif '/' not in image or image.count('/') == 1:
        # No registry specified, or just owner/image format (DockerHub)
        # Only if it doesn't contain a dot (which would indicate a registry domain)
        if '.' not in image.split('/')[0]:
            return 'dockerhub'
    
    # If we get here, it's likely a custom registry we don't support
    return 'unsupported'

def get_dockerhub_auth_token(repository: str) -> str:
    """Get Docker Hub authentication token."""
    url = "https://auth.docker.io/token"
    params = {
        "service": "registry.docker.io",
        "scope": f"repository:{repository}:pull"
    }
    try:
        response = requests.get(url, params=params, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()['token']
    except requests.exceptions.RequestException as e:
        print(f"Failed to get auth token: {e}", file=sys.stderr)
        sys.exit(1)

def get_ghcr_auth_token(repository: str) -> str:
    """Get GitHub Container Registry authentication token."""
    # First check if user provided a token
    user_token = os.environ.get('GITHUB_TOKEN')
    if user_token:
        return user_token
    
    # Otherwise, get an anonymous token for public images
    url = "https://ghcr.io/token"
    params = {
        "scope": f"repository:{repository}:pull"
    }
    try:
        response = requests.get(url, params=params, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()['token']
    except requests.exceptions.RequestException as e:
        print(f"Failed to get GHCR auth token: {e}", file=sys.stderr)
        sys.exit(1)

def get_quay_auth_token() -> Optional[str]:
    """Get Quay.io authentication token from environment."""
    # Quay.io can work anonymously for public images, but auth provides higher rate limits
    token = os.environ.get('QUAY_TOKEN')
    return token

def get_manifest_dockerhub(repository: str, tag: str, token: str) -> Dict:
    """Fetch manifest from Docker Hub."""
    headers = {
        'Accept': 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json',
        'Authorization': f'Bearer {token}'
    }
    url = f"https://registry-1.docker.io/v2/{repository}/manifests/{tag}"
    try:
        response = requests.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Failed to get manifest: {e}", file=sys.stderr)
        sys.exit(1)

def get_manifest_ghcr(repository: str, tag: str, token: str) -> Dict:
    """Fetch manifest from GitHub Container Registry."""
    headers = {
        'Accept': 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json',
        'Authorization': f'Bearer {token}'
    }
    
    url = f"https://ghcr.io/v2/{repository}/manifests/{tag}"
    try:
        response = requests.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Failed to get manifest: {e}", file=sys.stderr)
        sys.exit(1)

def get_manifest_quay(repository: str, tag: str, token: Optional[str] = None) -> Dict:
    """Fetch manifest from Quay.io."""
    headers = {
        'Accept': 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'
    }
    if token:
        headers['Authorization'] = f'Bearer {token}'
    
    url = f"https://quay.io/v2/{repository}/manifests/{tag}"
    try:
        response = requests.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Failed to get manifest: {e}", file=sys.stderr)
        sys.exit(1)

def get_config_blob(registry: str, repository: str, digest: str, token: str) -> Dict:
    """Fetch the config blob for a single-arch image."""
    if registry == 'dockerhub':
        url = f"https://registry-1.docker.io/v2/{repository}/blobs/{digest}"
        headers = {'Authorization': f'Bearer {token}'}
    elif registry == 'ghcr':
        url = f"https://ghcr.io/v2/{repository}/blobs/{digest}"
        headers = {'Authorization': f'Bearer {token}'}
    elif registry == 'quay':
        url = f"https://quay.io/v2/{repository}/blobs/{digest}"
        headers = {'Authorization': f'Bearer {token}'} if token else {}
    else:
        return {}
    
    try:
        response = requests.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException:
        return {}

def check_architectures(manifest: Dict, registry: str = None, repository: str = None, token: str = None) -> List[str]:
    """Check available architectures in the manifest."""
    # Multi-arch manifest (manifest list or OCI index)
    if manifest.get('manifests'):
        archs = [m['platform']['architecture'] for m in manifest['manifests'] if 'platform' in m]
        return archs
    # Single-arch manifest (Docker v2 or OCI manifest)
    elif manifest.get('config'):
        # Check if it's a Helm chart or other non-container artifact
        config_type = manifest.get('config', {}).get('mediaType', '')
        if 'helm' in config_type or 'artifact' in config_type:
            # Not a container image, cannot determine architecture from manifest alone
            return []
        
        # For single-arch container images, fetch the config blob to get architecture
        if registry and repository and token:
            digest = manifest['config'].get('digest')
            if digest:
                config_blob = get_config_blob(registry, repository, digest, token)
                arch = config_blob.get('architecture')
                if arch:
                    return [arch]
        
        # Fallback if we couldn't get the config
        return ['unknown']
    else:
        return []

def parse_image_spec(image: str, registry: str) -> Tuple[str, str]:
    """Parse image specification into repository and tag."""
    # Remove registry prefix if present
    if registry == 'ghcr':
        if image.startswith('ghcr.io/'):
            image = image[8:]  # Remove 'ghcr.io/' prefix
    elif registry == 'quay':
        if image.startswith('quay.io/'):
            image = image[8:]  # Remove 'quay.io/' prefix
    elif registry == 'dockerhub':
        if image.startswith('docker.io/'):
            image = image[10:]  # Remove 'docker.io/' prefix
    
    # Split image and tag
    if ':' in image:
        repository, tag = image.split(':', 1)
    else:
        repository, tag = image, 'latest'

    # For DockerHub, add 'library/' prefix for official images
    if registry == 'dockerhub' and '/' not in repository:
        repository = f'library/{repository}'
    
    return repository.lower(), tag

def get_manifest(image: str, registry: str, repository: str, tag: str) -> Dict:
    """Get manifest based on registry type."""
    if registry == 'dockerhub':
        token = get_dockerhub_auth_token(repository)
        return get_manifest_dockerhub(repository, tag, token)
    elif registry == 'ghcr':
        token = get_ghcr_auth_token(repository)
        return get_manifest_ghcr(repository, tag, token)
    elif registry == 'quay':
        token = get_quay_auth_token()
        return get_manifest_quay(repository, tag, token)
    else:
        print(f"Unsupported registry: {registry}", file=sys.stderr)
        sys.exit(1)

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Check container image architectures on DockerHub, GitHub Container Registry, or Quay.io',
        epilog='''
Examples:
  %(prog)s nginx:latest                    # DockerHub official image
  %(prog)s ubuntu/nginx:latest             # DockerHub image
  %(prog)s ghcr.io/owner/image:latest      # GitHub Container Registry image
  %(prog)s quay.io/namespace/image:latest  # Quay.io image
  
Note: For authentication (optional), set GITHUB_TOKEN for GHCR, or QUAY_TOKEN for Quay.io.
        ''',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('image', help='Container image name (format: [registry/]name:tag)')
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    
    # Detect registry type
    registry = detect_registry(args.image)
    
    # Check if registry is supported
    if registry == 'unsupported':
        # Extract registry name from image
        registry_name = args.image.split('/')[0] if '/' in args.image else args.image
        print(f"✗ Unsupported registry: {registry_name}", file=sys.stderr)
        print(f"Supported registries: DockerHub, ghcr.io, quay.io", file=sys.stderr)
        sys.exit(1)
    
    # Parse image specification
    repository, tag = parse_image_spec(args.image, registry)
    
    # Get manifest and token for potential config blob fetching
    if registry == 'dockerhub':
        token = get_dockerhub_auth_token(repository)
    elif registry == 'ghcr':
        token = get_ghcr_auth_token(repository)
    elif registry == 'quay':
        token = get_quay_auth_token()
    else:
        token = None
    
    manifest = get_manifest(args.image, registry, repository, tag)
    architectures = check_architectures(manifest, registry, repository, token)

    registry_labels = {'ghcr': 'GHCR', 'quay': 'Quay.io', 'dockerhub': 'DockerHub'}
    registry_label = registry_labels.get(registry, registry)

    if not architectures:
        # Check if it's a non-container artifact (Helm chart, etc.)
        config_type = manifest.get('config', {}).get('mediaType', '')
        if 'helm' in config_type:
            print(f"⚠ {args.image} ({registry_label}) is a Helm chart, not a container image", file=sys.stderr)
        elif 'artifact' in config_type or manifest.get('artifactType'):
            print(f"⚠ {args.image} ({registry_label}) is an OCI artifact, not a container image", file=sys.stderr)
        else:
            print(f"No architectures found for {args.image}", file=sys.stderr)
        sys.exit(1)

    available_targets = TARGET_ARCHITECTURES.intersection(architectures)
    missing_targets = TARGET_ARCHITECTURES - set(architectures)
    
    if not missing_targets:
        print(f"✓ Image {args.image} ({registry_label}) supports all required architectures")
    else:
        print(f"✗ Image {args.image} ({registry_label}) is missing architectures: {', '.join(missing_targets)}")
        print(f"Available architectures: {', '.join(architectures)}")
