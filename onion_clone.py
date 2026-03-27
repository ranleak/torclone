import modal
import subprocess
import tempfile
import os
import shlex
import urllib.parse
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

# Define the Modal App
app = modal.App("tor-httrack-downloader")

# Define a Volume for persistent storage
volume = modal.Volume.from_name("onion-archives", create_if_missing=True)
VOLUME_DIR = "/data"

# Define the container image with the required dependencies
image = (
    modal.Image.debian_slim()
    .apt_install("tor", "torsocks", "httrack", "zip")
    .pip_install("fastapi[standard]")
)

@app.function(image=image, timeout=3600, volumes={VOLUME_DIR: volume})  # 1 hour timeout for large mirrors
def download_onion(url: str, httrack_opts: str = "") -> bytes:
    """
    Starts Tor, waits for it to connect, and runs HTTrack through Torsocks.
    Returns the zipped website archive as bytes.
    """
    print("Starting Tor daemon...")
    # Start Tor in the background
    tor_process = subprocess.Popen(
        ["tor"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    print("Waiting for Tor to bootstrap (this may take a minute)...")
    bootstrapped = False
    
    # Read Tor logs until we confirm it has successfully connected to the network
    for line in iter(tor_process.stdout.readline, ''):
        if "Bootstrapped 100%" in line:
            print("Tor successfully bootstrapped!")
            bootstrapped = True
            break
        # Print important Tor progress updates or errors
        if "Bootstrapped" in line or "[WARN]" in line or "[ERR]" in line:
            print(f"Tor: {line.strip()}")

    if not bootstrapped:
        tor_process.terminate()
        raise RuntimeError("Failed to bootstrap Tor circuit.")

    # Extract domain from URL to create a specific folder in the volume
    domain = urllib.parse.urlparse(url).netloc
    dest_dir = os.path.join(VOLUME_DIR, domain)
    os.makedirs(dest_dir, exist_ok=True)

    try:
        # Construct the HTTrack command, prefixed with 'torsocks'
        cmd = ["torsocks", "httrack", url, "-O", dest_dir]
        
        # Safely parse and append any user-provided options
        if httrack_opts:
            cmd.extend(shlex.split(httrack_opts))

        print(f"Running HTTrack: {' '.join(cmd)}")
        
        # Run the command
        result = subprocess.run(cmd, capture_output=True, text=True)

        print("--- HTTrack Output ---")
        # HTTrack can be noisy, so we'll output the tail of the logs if needed, 
        # or just rely on Modal's logging.
        print(result.stdout[:1000] + "\n...\n" + result.stdout[-1000:] if len(result.stdout) > 2000 else result.stdout)
        
        if result.stderr:
            print("--- HTTrack Errors ---")
            print(result.stderr)

        if result.returncode != 0:
            print(f"Warning: HTTrack exited with non-zero code {result.returncode}")

        # Commit the volume to save changes persistently!
        volume.commit()

        # Zip the contents of the output directory
        zip_path = "/tmp/website_archive.zip"
        print(f"Zipping contents to {zip_path}...")
        subprocess.run(["zip", "-r", zip_path, "."], cwd=dest_dir, check=True, capture_output=True)

        # Read the zipped file into memory so we can return it over the network
        with open(zip_path, "rb") as f:
            archive_bytes = f.read()

        # Clean up the zip file from the container
        os.remove(zip_path)

        return archive_bytes
        
    finally:
        # Ensure Tor is killed when the function finishes or fails
        print("Cleaning up and terminating Tor...")
        tor_process.terminate()
        tor_process.wait()

web_app = FastAPI()

@web_app.get("/")
def list_archives():
    """Lists all downloaded domains available in the volume."""
    if not os.path.exists(VOLUME_DIR):
        return HTMLResponse("<h1>No archives found yet.</h1>")
    
    # Reload volume to ensure we see the latest files
    volume.reload()
    
    dirs = [d for d in os.listdir(VOLUME_DIR) if os.path.isdir(os.path.join(VOLUME_DIR, d))]
    if not dirs:
        return HTMLResponse("<h1>No archives found yet.</h1>")
        
    links = "".join([f'<li><a href="/sites/{d}/">{d}</a></li>' for d in dirs])
    return HTMLResponse(f"<h1>Archived Sites</h1><ul>{links}</ul>")

@app.function(volumes={VOLUME_DIR: volume})
@modal.asgi_app()
def serve_archives():
    """
    Web endpoint to preview the downloaded sites.
    """
    # Reload volume on startup
    volume.reload()
    os.makedirs(VOLUME_DIR, exist_ok=True)
    
    # Mount the volume directory so it can be served as static files
    web_app.mount("/sites", StaticFiles(directory=VOLUME_DIR, html=True), name="sites")
    return web_app

@app.local_entrypoint()
def main(url: str, opts: str = "-r1 -w", output: str = "onion_archive.zip"):
    """
    Local entrypoint to invoke the Modal function from your terminal.
    
    Usage examples:
        modal run onion_scraper.py --url "http://example.onion"
        modal run onion_scraper.py --url "http://example.onion" --opts "-r2 -w -s0" --output "my_site.zip"
        
    Options used in the default `-r1 -w`:
        -rN: Set mirror depth to N
        -w: Mirror web sites
    """
    print(f"Requesting download of {url}")
    print(f"HTTrack options: '{opts}'")
    print("\nNote: To host your downloaded sites on a live URL, run: modal deploy onion_scraper.py")
    print("Then visit the `.modal.run` URL provided for 'serve_archives' in the terminal.\n")
    
    # Execute the function remotely on Modal's infrastructure
    archive_data = download_onion.remote(url, opts)

    if archive_data:
        # Write the returned bytes to a local file
        with open(output, "wb") as f:
            f.write(archive_data)
        print(f"\n✅ Successfully downloaded and saved to {output} ({len(archive_data) / 1024 / 1024:.2f} MB).")
    else:
        print("\n❌ Failed to retrieve archive data.")