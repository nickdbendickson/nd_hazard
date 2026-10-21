"""Open a Processing tool's dialog in front of the window that asked for it.

processing.execAlgorithmDialog has two problems when a plugin window opens a tool:
- it parents the tool's dialog to the QGIS main window, so the window that asked (the Runner
  or Screening window, the Runner wizard) is only a sibling and a click on it raises it over
  the tool's dialog;
- it calls show() before exec_(), and Qt only makes a dialog modal when it is shown by
  exec_(), so the tool's dialog is not modal at all. Opened from the wizard (which is modal)
  the wizard then blocks the tool's dialog: the wizard pops up in front and nothing in the
  tool's dialog can be clicked.
exec_tool_dialog makes the dialog a child window of the asking window and shows it with
exec_() alone, so it is modal and stays in front of that window. Otherwise it does what
execAlgorithmDialog does.

Edit shared/ui/tool_dialog.py only, then run tools/sync_ui.py.
"""


def exec_tool_dialog(processing, alg_id, params=None, parent=None):
    """processing.execAlgorithmDialog(alg_id, params), with the dialog owned by `parent`'s
    window. Returns the tool's results (empty when it was closed without a run). Falls back to
    execAlgorithmDialog when there is no parent or the processing module has no
    createAlgorithmDialog (QGIS before 3.10, the tests' fakes)."""
    params = dict(params or {})
    create = getattr(processing, "createAlgorithmDialog", None)
    owner = parent.window() if parent is not None else None
    if create is None or owner is None:
        return processing.execAlgorithmDialog(alg_id, params)
    dlg = create(alg_id, params)
    if dlg is None:
        return {}
    dlg.setParent(owner, dlg.windowFlags())
    canvas = prev_tool = None
    try:
        from qgis.utils import iface
        canvas = iface.mapCanvas() if iface is not None else None
        prev_tool = canvas.mapTool() if canvas is not None else None
    except Exception:                                            # noqa
        canvas = None
    (getattr(dlg, "exec_", None) or dlg.exec)()
    # as execAlgorithmDialog: put back the map tool a point/extent picker took over
    if canvas is not None and canvas.mapTool() != prev_tool:
        try:
            canvas.mapTool().reset()
        except Exception:                                        # noqa
            pass
        canvas.setMapTool(prev_tool)
    results = dlg.results()
    dlg.close()
    dlg.deleteLater()
    return results
