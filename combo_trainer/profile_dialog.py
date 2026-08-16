"""
profile_dialog.py — Game profile / layout picker and button rebinding modal.

Layout of the modal, top to bottom:

  1. Game  ▾   Layout ▾   [New…] [Duplicate…] [Rename…] [Delete]   SOCD ▾
  2. A row per physical switch on the box: label, XInput source, and the
     logical input(s) it fires. Rows pulse while their switch is held, and
     "Listen" lets the user select a row by pressing the switch instead of
     hunting for it in a list.
  3. Bind panel: pick a logical input, then Set (replace) / Add (make it a
     macro) / Clear (leave the switch inert).
  4. A live validation line — the 16-vs-13 story in one sentence:
     "fully mapped · free buttons: 1 · duplicates: 1 · macros: 1".

Every mutation goes through ProfileManager, which notifies MainWindow, which
pushes a freshly resolved profile into the input thread. Rebinds are
therefore live: press the switch right after binding it and the Controller
Test readout already shows the new input.

Like `controller_dialog.py`, this never touches XInput itself — it renders
the InputThread's monitor stream so all device I/O stays on one thread.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .input_engine import InputThread, PadSnapshot
from .motion_parser import SOCD_NEUTRAL, SOCD_UP_PRIORITY
from .profiles import InputCategory, ProfileError, ProfileManager

_COL_SWITCH, _COL_SOURCE, _COL_BOUND = 0, 1, 2

_SOCD_CHOICES = (
    ("Neutral  (L+R → 5, U+D → 5)", SOCD_NEUTRAL),
    ("Up priority  (L+R → 5, U+D → 8)", SOCD_UP_PRIORITY),
)


class ProfileDialog(QDialog):
    def __init__(
        self,
        manager: ProfileManager,
        input_thread: InputThread | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Profiles & Rebinding")
        self.setModal(True)
        self.setMinimumSize(720, 620)

        self._mgr = manager
        self._thread = input_thread
        self._refreshing = False  # guards combo signals during programmatic sets
        self._pressed: set[str] = set()

        self._build_ui()
        self._reload_games()

        self._mgr.add_listener(self._on_manager_changed)
        if self._thread is not None:
            self._thread.monitor_state.connect(self._on_snapshots)
            self._thread.raw_press.connect(self._on_raw_press)
            self._thread.set_monitoring(True)
        self.finished.connect(self._teardown)

    # -- UI ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # -- profile pickers -------------------------------------------------
        picker = QGroupBox("Profile")
        pick_lay = QVBoxLayout(picker)

        row = QHBoxLayout()
        row.addWidget(QLabel("Game:"))
        self._game_box = QComboBox()
        self._game_box.currentIndexChanged.connect(self._on_game_picked)
        row.addWidget(self._game_box, stretch=1)
        row.addWidget(QLabel("Layout:"))
        self._layout_box = QComboBox()
        self._layout_box.currentIndexChanged.connect(self._on_layout_picked)
        row.addWidget(self._layout_box, stretch=1)
        pick_lay.addLayout(row)

        row2 = QHBoxLayout()
        for text, slot in (
            ("New…", self._new_layout),
            ("Duplicate…", self._duplicate_layout),
            ("Rename…", self._rename_layout),
            ("Delete", self._delete_layout),
        ):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            row2.addWidget(btn)
        row2.addStretch(1)
        row2.addWidget(QLabel("SOCD:"))
        self._socd_box = QComboBox()
        for text, mode in _SOCD_CHOICES:
            self._socd_box.addItem(text, userData=mode)
        self._socd_box.currentIndexChanged.connect(self._on_socd_picked)
        row2.addWidget(self._socd_box)
        pick_lay.addLayout(row2)

        self._notes_label = QLabel("")
        self._notes_label.setWordWrap(True)
        self._notes_label.setStyleSheet("color: #9a9aa4;")
        pick_lay.addWidget(self._notes_label)
        root.addWidget(picker)

        # -- binding table ----------------------------------------------------
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Switch", "XInput source", "Bound to"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(_COL_SWITCH, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_SOURCE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_BOUND, QHeaderView.ResizeMode.Stretch)
        self._table.itemSelectionChanged.connect(self._sync_bind_panel)
        root.addWidget(self._table, stretch=1)

        # -- bind panel --------------------------------------------------------
        bind_box = QGroupBox("Rebind selected switch")
        bind_lay = QVBoxLayout(bind_box)

        row3 = QHBoxLayout()
        self._input_box = QComboBox()
        row3.addWidget(self._input_box, stretch=1)
        for text, slot, tip in (
            ("Set", self._set_binding, "Replace this switch's binding"),
            ("Add", self._add_binding, "Also fire this input — makes it a macro"),
            ("Clear", self._clear_binding, "Leave the switch unmapped"),
        ):
            btn = QPushButton(text)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            row3.addWidget(btn)
        bind_lay.addLayout(row3)

        row4 = QHBoxLayout()
        self._listen_check = QCheckBox("Listen — press a switch on the stick to select it")
        self._listen_check.toggled.connect(self._on_listen_toggled)
        row4.addWidget(self._listen_check)
        row4.addStretch(1)
        restore = QPushButton("Restore Defaults…")
        restore.clicked.connect(self._restore_defaults)
        row4.addWidget(restore)
        bind_lay.addLayout(row4)
        root.addWidget(bind_box)

        self._status = QLabel("")
        root.addWidget(self._status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Save Profiles")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # -- population ------------------------------------------------------------

    def _reload_games(self) -> None:
        self._refreshing = True
        self._game_box.clear()
        for game in self._mgr.games.values():
            self._game_box.addItem(game.name, userData=game.id)
        idx = self._game_box.findData(self._mgr.active_game_id)
        self._game_box.setCurrentIndex(max(idx, 0))
        self._refreshing = False
        self._reload_layouts()

    def _reload_layouts(self) -> None:
        game = self._mgr.active_game
        self._refreshing = True
        self._layout_box.clear()
        if game is not None:
            for layout in game.layouts.values():
                text = layout.name
                if layout.character:
                    text += f"  ({layout.character})"
                if layout.id == game.default_layout_id:
                    text += "  ★"
                self._layout_box.addItem(text, userData=layout.id)
            idx = self._layout_box.findData(self._mgr.active_layout_id)
            self._layout_box.setCurrentIndex(max(idx, 0))
        self._refreshing = False

        self._sync_layout_details()
        self._reload_inputs()
        self._reload_table()

    def _sync_layout_details(self) -> None:
        """SOCD mode and notes are per-layout, so they follow every switch."""
        layout = self._mgr.active_layout
        if layout is None:
            return
        self._refreshing = True
        socd_idx = self._socd_box.findData(layout.socd_mode)
        self._socd_box.setCurrentIndex(max(socd_idx, 0))
        self._notes_label.setText(layout.notes)
        self._refreshing = False

    def _reload_inputs(self) -> None:
        """Populate the logical-input picker from the active game's vocabulary."""
        game = self._mgr.active_game
        selected = self._input_box.currentData()
        self._input_box.clear()
        if game is None:
            return
        for category in (
            InputCategory.DIRECTION,
            InputCategory.ATTACK,
            InputCategory.MECHANIC,
            InputCategory.SYSTEM,
        ):
            for li in game.inputs_in(category):
                self._input_box.addItem(
                    f"{li.name}   ({category.value.lower()})", userData=li.id
                )
        idx = self._input_box.findData(selected)
        if idx >= 0:
            self._input_box.setCurrentIndex(idx)

    def _reload_table(self) -> None:
        device, layout = self._mgr.active_device, self._mgr.active_layout
        game = self._mgr.active_game
        selected = self._selected_physical_id()

        self._table.setRowCount(0)
        if device is None or layout is None or game is None:
            return

        self._table.setRowCount(len(device))
        for row, btn in enumerate(device):
            switch = QTableWidgetItem(f"{btn.label}   [{btn.group}]")
            switch.setData(Qt.ItemDataRole.UserRole, btn.id)
            self._table.setItem(row, _COL_SWITCH, switch)
            self._table.setItem(row, _COL_SOURCE, QTableWidgetItem(btn.source))

            logical_ids = layout.logical_for(btn.id)
            names = [
                (game.input(lid).name if game.input(lid) else f"{lid} (unknown!)")
                for lid in logical_ids
            ]
            if not names:
                text = "— unmapped —"
            elif len(names) == 1:
                text = names[0]
            else:
                text = " + ".join(names) + "   (macro)"
            # Flag a shared input so duplicates read as deliberate, not a bug.
            extra = [
                lid
                for lid in logical_ids
                if len(layout.physicals_for(lid)) > 1
            ]
            if extra:
                text += "   (also on another switch)"
            bound = QTableWidgetItem(text)
            if not names:
                bound.setToolTip("This switch sends nothing to the game.")
            self._table.setItem(row, _COL_BOUND, bound)
            if not names:
                for col in range(3):
                    self._table.item(row, col).setForeground(QColor("#6f6f79"))

        self._select_physical(selected or device.ids[0])
        self._refresh_status()

    def _refresh_status(self) -> None:
        try:
            validation = self._mgr.validate()
        except ProfileError as exc:
            self._status.setText(str(exc))
            return
        prefix = "✓" if validation.ok else "⚠"
        self._status.setText(f"{prefix}  {validation.summary()}")
        self._status.setStyleSheet(
            "color: #6ee87e;" if validation.ok else "color: #ffb454;"
        )

    # -- selection helpers -------------------------------------------------------

    def _selected_physical_id(self) -> str | None:
        items = self._table.selectedItems()
        if not items:
            return None
        item = self._table.item(items[0].row(), _COL_SWITCH)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _select_physical(self, physical_id: str) -> None:
        for row in range(self._table.rowCount()):
            item = self._table.item(row, _COL_SWITCH)
            if item and item.data(Qt.ItemDataRole.UserRole) == physical_id:
                self._table.selectRow(row)
                return

    def _sync_bind_panel(self) -> None:
        """Preselect whatever the highlighted switch already fires."""
        layout = self._mgr.active_layout
        physical_id = self._selected_physical_id()
        if layout is None or physical_id is None:
            return
        bound = layout.logical_for(physical_id)
        if bound:
            idx = self._input_box.findData(bound[0])
            if idx >= 0:
                self._input_box.setCurrentIndex(idx)

    # -- profile picker slots -------------------------------------------------------

    def _on_game_picked(self, _index: int) -> None:
        if self._refreshing:
            return
        game_id = self._game_box.currentData()
        if game_id and game_id != self._mgr.active_game_id:
            self._mgr.activate(game_id)

    def _on_layout_picked(self, _index: int) -> None:
        if self._refreshing:
            return
        layout_id = self._layout_box.currentData()
        if layout_id and layout_id != self._mgr.active_layout_id:
            self._mgr.set_active_layout(layout_id)

    def _on_socd_picked(self, _index: int) -> None:
        if self._refreshing:
            return
        mode = self._socd_box.currentData()
        if mode and self._mgr.active_layout is not None:
            self._mgr.set_socd_mode(mode)

    # -- layout lifecycle slots -------------------------------------------------------

    def _ask_name(self, title: str, default: str = "") -> str:
        name, ok = QInputDialog.getText(self, title, "Layout name:", text=default)
        return name.strip() if ok else ""

    def _new_layout(self) -> None:
        name = self._ask_name("New layout")
        if not name:
            return
        self._create(name, copy_from=None)

    def _duplicate_layout(self) -> None:
        layout = self._mgr.active_layout
        if layout is None:
            return
        name = self._ask_name("Duplicate layout", f"{layout.name} copy")
        if not name:
            return
        self._create(name, copy_from=layout.id)

    def _create(self, name: str, copy_from: str | None) -> None:
        character, _ = QInputDialog.getText(
            self,
            "Character (optional)",
            "Bind this layout to a character?\n"
            "Combo files whose 'character' field matches will select it "
            "automatically. Leave blank for none.",
        )
        try:
            layout = self._mgr.create_layout(
                name, copy_from=copy_from, character=character.strip()
            )
        except ProfileError as exc:
            QMessageBox.warning(self, "Cannot create layout", str(exc))
            return
        self._mgr.set_active_layout(layout.id)

    def _rename_layout(self) -> None:
        layout = self._mgr.active_layout
        if layout is None:
            return
        name = self._ask_name("Rename layout", layout.name)
        if name:
            self._mgr.rename_layout(layout.id, name)

    def _delete_layout(self) -> None:
        layout = self._mgr.active_layout
        if layout is None:
            return
        confirm = QMessageBox.question(
            self,
            "Delete layout",
            f"Delete layout '{layout.name}'? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm is not QMessageBox.StandardButton.Yes:
            return
        try:
            self._mgr.delete_layout(layout.id)
        except ProfileError as exc:
            QMessageBox.warning(self, "Cannot delete layout", str(exc))

    def _restore_defaults(self) -> None:
        confirm = QMessageBox.question(
            self,
            "Restore defaults",
            "Replace the built-in device and game profiles with the shipped "
            "defaults? Layouts you created yourself are kept, but edits to the "
            "built-in ones are lost.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm is not QMessageBox.StandardButton.Yes:
            return
        from .games import seed_builtins

        seed_builtins(self._mgr, overwrite=True)
        self._reload_games()

    # -- rebinding slots ----------------------------------------------------------

    def _selected_or_warn(self) -> str | None:
        physical_id = self._selected_physical_id()
        if physical_id is None:
            QMessageBox.information(
                self, "No switch selected", "Pick a switch in the list first."
            )
        return physical_id

    def _set_binding(self) -> None:
        physical_id = self._selected_or_warn()
        logical_id = self._input_box.currentData()
        if physical_id and logical_id:
            self._apply(lambda: self._mgr.bind(physical_id, logical_id))

    def _add_binding(self) -> None:
        physical_id = self._selected_or_warn()
        logical_id = self._input_box.currentData()
        if physical_id and logical_id:
            self._apply(lambda: self._mgr.add_to_macro(physical_id, logical_id))

    def _clear_binding(self) -> None:
        physical_id = self._selected_or_warn()
        if physical_id:
            self._apply(lambda: self._mgr.unbind(physical_id))

    def _apply(self, fn) -> None:
        try:
            fn()
        except ProfileError as exc:
            QMessageBox.warning(self, "Cannot rebind", str(exc))

    # -- live device feedback ------------------------------------------------------

    def _on_listen_toggled(self, enabled: bool) -> None:
        if self._thread is not None:
            self._thread.set_listening(enabled)

    def _on_raw_press(self, source: str) -> None:
        """Press-to-select: jump to the row owning the switch just pressed."""
        if not self._listen_check.isChecked():
            return
        device = self._mgr.active_device
        if device is None:
            return
        btn = device.by_source(source)
        if btn is not None:
            self._select_physical(btn.id)

    def _on_snapshots(self, snaps: list[PadSnapshot]) -> None:
        """Pulse the source cell of every switch currently held."""
        device = self._mgr.active_device
        if device is None or not snaps:
            return
        snap = next((s for s in snaps if s.connected), None)
        pressed = (
            {b.source for b in device if snap.pressed(b.source)} if snap else set()
        )
        if pressed == self._pressed:
            return
        self._pressed = pressed
        for row in range(self._table.rowCount()):
            item = self._table.item(row, _COL_SOURCE)
            switch = self._table.item(row, _COL_SWITCH)
            if item is None or switch is None:
                continue
            btn = device.get(switch.data(Qt.ItemDataRole.UserRole))
            if btn is None:
                continue
            font = item.font()
            font.setBold(btn.source in pressed)
            item.setFont(font)

    # -- persistence / lifecycle ------------------------------------------------------

    def _save(self) -> None:
        try:
            path = self._mgr.save()
        except OSError as exc:
            QMessageBox.critical(self, "Could not save profiles", str(exc))
            return
        self._status.setText(f"✓  Saved to {path}")
        self.accept()

    def _on_manager_changed(self, _mgr: ProfileManager) -> None:
        """Refresh after ANY mutation — ours, or one from the main window."""
        if self._refreshing:
            return
        game_ids = [
            self._game_box.itemData(i) for i in range(self._game_box.count())
        ]
        if game_ids != list(self._mgr.games):
            self._reload_games()
            return
        layout_ids = [
            self._layout_box.itemData(i) for i in range(self._layout_box.count())
        ]
        game = self._mgr.active_game
        if (
            self._game_box.currentData() != self._mgr.active_game_id
            or game is None
            or layout_ids != list(game.layouts)
            or self._layout_box.currentData() != self._mgr.active_layout_id
        ):
            self._reload_layouts()
        else:
            # Same game and layout list: refresh in place. Clearing the combo
            # boxes here would destroy the widget mid-signal.
            self._sync_layout_details()
            self._reload_table()

    def _teardown(self) -> None:
        self._mgr.remove_listener(self._on_manager_changed)
        if self._thread is None:
            return
        self._thread.set_listening(False)
        self._thread.set_monitoring(False)
        try:
            self._thread.monitor_state.disconnect(self._on_snapshots)
            self._thread.raw_press.disconnect(self._on_raw_press)
        except TypeError:
            pass  # already disconnected
