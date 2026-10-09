"""The one Jinja loader. Pages extend templates/base.html."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from ops_ui.present import stage_href

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
TEMPLATES.env.globals["stage_href"] = stage_href
