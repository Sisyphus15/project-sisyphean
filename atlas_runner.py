import os
import subprocess


def run_atlas_build(atlas_dir: str) -> tuple[int, str, str, str]:
    """
    Runs: python3 ingest_maps.py
    Returns: (returncode, stdout, stderr, output_png_path)
    """
    output_png_path = os.path.join(atlas_dir, "output", "dashboard.png")

    proc = subprocess.run(
        ["python3", "ingest_maps.py"],
        cwd=atlas_dir,
        capture_output=True,
        text=True,
    )

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    return proc.returncode, stdout, stderr, output_png_path
