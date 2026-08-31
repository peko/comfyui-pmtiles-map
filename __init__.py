"""comfyui-pmtiles-map -- save renders into a PMTiles map, browse it in Leaflet.

Nodes:  SavePMTilesMap, PMTilesMapInfo   (category image/pmtiles)
Viewer: http://<comfyui>/map/

Route registration is guarded because tools/make_workflow.load_node_defs()
imports every pack headlessly, with no PromptServer.instance -- an unguarded
registration there would break workflow generation.  There is deliberately no
WEB_DIRECTORY: ComfyUI auto-loads every .js under it into its own frontend
(ComfyUI/server.py:1243), and the viewer is a standalone page, not a UI
extension.  Its assets are served by our own /map/assets/ route instead.
"""
from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

try:
    from . import routes
    routes.register()
except Exception as exc:                # never take ComfyUI down over a viewer
    print(f"[pmtiles-map] route registration skipped: {type(exc).__name__}: {exc}")

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
