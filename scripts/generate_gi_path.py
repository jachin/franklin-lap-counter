import json
import os
import platform

"""
Generates shell export commands for GI_TYPELIB_PATH and XDG_DATA_DIRS.
This script extracts relevant Nix store paths from devbox.lock to ensure 
that only the libraries and typelibs intended for this project are loaded, 
avoiding conflicts with other versions present in the system's Nix store.
"""

def main():
    with open("devbox.lock") as f:
        lock = json.load(f)
    
    gi_paths = []
    xdg_paths = []
    # Identify the current system
    import platform
    machine = platform.machine()
    system_name = platform.system().lower()
    
    if system_name == "darwin":
        system = f"{machine}-darwin"
    else:
        system = f"{machine}-linux"
    
    # Map x86_64 to x86_64 and arm64/aarch64 to aarch64
    if "x86_64" in system:
        system = system.replace("x86_64", "x86_64")
    elif "arm64" in system or "aarch64" in system:
        if "darwin" in system:
            system = "aarch64-darwin"
        else:
            system = "aarch64-linux"
    
    for pkg_name, pkg_info in lock.get("packages", {}).items():
        systems = pkg_info.get("systems", {})
        if system in systems:
            outputs = systems[system].get("outputs", [])
            for out in outputs:
                path = out.get("path")
                if path:
                    gi_path = os.path.join(path, "lib", "girepository-1.0")
                    if os.path.isdir(gi_path):
                        gi_paths.append(gi_path)
                    
                    share_path = os.path.join(path, "share")
                    if os.path.isdir(share_path):
                        xdg_paths.append(share_path)
    
    # Also add the profile paths as fallback/primary
    profile_base = os.path.abspath(".devbox/nix/profile/default")
    profile_gi_path = os.path.join(profile_base, "lib", "girepository-1.0")
    if os.path.isdir(profile_gi_path) and profile_gi_path not in gi_paths:
        gi_paths.insert(0, profile_gi_path)
    
    profile_share_path = os.path.join(profile_base, "share")
    if os.path.isdir(profile_share_path) and profile_share_path not in xdg_paths:
        xdg_paths.insert(0, profile_share_path)
        
    print(f"export GI_TYPELIB_PATH=\"{':'.join(gi_paths)}:$GI_TYPELIB_PATH\"")
    print(f"export XDG_DATA_DIRS=\"{':'.join(xdg_paths)}:$XDG_DATA_DIRS\"")

if __name__ == "__main__":
    main()
