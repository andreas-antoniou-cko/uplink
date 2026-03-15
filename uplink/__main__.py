"""
Uplink — Claude Code history viewer.

Run in any project directory:
    uplink

Opens a browser tab at http://localhost:5000 showing all Claude Code sessions
recorded for that directory.
"""
import os
import threading
import time
import webbrowser

import click

from uplink.server import create_app


@click.command()
@click.option("--port", default=5000, show_default=True, help="Port to listen on.")
@click.option("--no-browser", is_flag=True, default=False, help="Don't open the browser automatically.")
@click.option("--dir", "project_dir", default=None, help="Project directory to show history for (defaults to cwd).")
def main(port: int, no_browser: bool, project_dir: str | None) -> None:
    """Browse Claude Code conversation history for the current project."""
    target_dir = os.path.abspath(project_dir or os.getcwd())

    app = create_app(target_dir)

    if not no_browser:
        url = f"http://localhost:{port}"
        # Open browser slightly after Flask starts.
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        click.echo(f"  uplink  →  {url}")
        click.echo(f"  project: {target_dir}")
        click.echo("  Press Ctrl+C to stop.\n")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
