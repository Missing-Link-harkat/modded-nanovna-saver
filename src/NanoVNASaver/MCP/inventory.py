#  NanoVNASaver
#
#  A python program to view and export Touchstone data from a NanoVNA
#  Copyright (C) 2019, 2020  Rune B. Broberg
#  Copyright (C) 2020,2021 NanoVNA-Saver Authors
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""Inventory: every GUI control and the MCP action that covers it.

Key: "<file>: <source line of the .clicked/.toggled/.triggered.connect>".
Value: action name(s) separated by "," or "-" + the reason the control
is deliberately not exposed. tests/test_mcp_inventory.py scans the
source; a new button without an entry here fails the test, so adding GUI
functionality forces a decision about the AI side too.
"""

INVENTORY: dict[str, str] = {
    # --- main window -----------------------------------------------------
    "NanoVNASaver.py: btnOpenCalibrationWindow.clicked.connect(":
        "- opens a window; calibration_* actions cover its content",
    'NanoVNASaver.py: btn_about.clicked.connect(lambda: self.display_window("about"))':
        "- about box, no function",
    'NanoVNASaver.py: btn_display_setup.clicked.connect(lambda: self.display_window("setup"))':
        "- opens a window; marker_add/marker_remove cover its functions",
    "NanoVNASaver.py: btn_open_file_window.clicked.connect(":
        "- opens a window; file_* actions cover its content",
    "NanoVNASaver.py: btn_set_reference.clicked.connect(self.setReference)":
        "reference_set",
    "NanoVNASaver.py: btn_show_analysis.clicked.connect(":
        "- opens a window; analysis_run",
    "NanoVNASaver.py: self.btnResetReference.clicked.connect(self.resetReference)":
        "reference_reset",
    'NanoVNASaver.py: self.tdr_button.clicked.connect(lambda: self.display_window("tdr"))':
        "tdr_result",
    # --- controls --------------------------------------------------------
    "Controls/MarkerControl.py: self.check_delta.toggled.connect(self.toggle_delta)":
        "- display only; marker_list gives values of every marker",
    "Controls/MarkerControl.py: self.check_delta_reference.toggled.connect(self.toggle_delta_reference)":
        "- display only; data_summary and reference cover comparison",
    "Controls/MarkerControl.py: self.showMarkerButton.clicked.connect(self.toggle_frame)":
        "- layout only",
    "Controls/SerialControl.py: self.btn_rescan.clicked.connect(self.rescanSerialPort)":
        "device_list_ports",
    "Controls/SerialControl.py: self.btn_settings.clicked.connect(":
        "- opens a window; device_* actions",
    "Controls/SerialControl.py: self.btn_toggle.clicked.connect(self.serialButtonClick)":
        "device_connect,device_disconnect",
    "Controls/SweepControl.py: btn.clicked.connect(self.app.sweep_start)":
        "sweep_run",
    "Controls/SweepControl.py: btn.clicked.connect(self.app.worker.quit)":
        "sweep_stop",
    "Controls/SweepControl.py: btn_settings_window.clicked.connect(":
        "- opens a window; sweep_configure",
    "Controls/SweepControl.py: self.btn_custom_sweep.clicked.connect(":
        "- opens a window; sweep_segmented",
    # --- windows ---------------------------------------------------------
    "Windows/About.py: self.ui.btn_copy_runtime_info.clicked.connect(self.copy_runtime_info)":
        "- clipboard of the user",
    "Windows/About.py: self.ui.btn_updates.clicked.connect(self.find_updates)":
        "- network access to GitHub, not a measurement function",
    "Windows/AnalysisWindow.py: btn_run_analysis.clicked.connect(self.runAnalysis)":
        "analysis_run",
    "Windows/Bands.py: btn_add_row.clicked.connect(self.app.bands.addRow)":
        "- edits stored band table (persistent settings); later issue",
    "Windows/Bands.py: btn_delete_row.clicked.connect(self.deleteRows)":
        "- edits stored band table (persistent settings); later issue",
    "Windows/Bands.py: btn_reset_bands.clicked.connect(self.resetBands)":
        "- edits stored band table (persistent settings); later issue",
    "Windows/CalibrationSettings.py: btn_apply.clicked.connect(self.calculate)":
        "calibration_apply",
    "Windows/CalibrationSettings.py: btn_delete_standard.clicked.connect(self.deleteCalibrationStandard)":
        "- deletes stored calibration kit definitions; human only",
    "Windows/CalibrationSettings.py: btn_load_file.clicked.connect(lambda: self.loadCalibration())":
        "calibration_load",
    "Windows/CalibrationSettings.py: btn_load_standard.clicked.connect(self.loadCalibrationStandard)":
        "- custom calibration kit coefficients; later issue",
    "Windows/CalibrationSettings.py: btn_reset.clicked.connect(self.reset)":
        "calibration_reset",
    "Windows/CalibrationSettings.py: btn_save_file.clicked.connect(lambda: self.saveCalibration())":
        "calibration_save",
    "Windows/CalibrationSettings.py: btn_save_standard.clicked.connect(self.saveCalibrationStandard)":
        "- stores calibration kit definitions; human only",
    "Windows/CalibrationSettings.py: cal_btn[label_name].clicked.connect(":
        "calibration_capture",
    "Windows/CalibrationSettings.py: self.btn_automatic.clicked.connect(self.automaticCalibration)":
        "calibration_capture",
    "Windows/CalibrationSettings.py: self.file_button_load.clicked.connect(self.select_file_load)":
        "- measured standard files; later issue",
    "Windows/CalibrationSettings.py: self.file_button_open.clicked.connect(self.select_file_open)":
        "- measured standard files; later issue",
    "Windows/CalibrationSettings.py: self.file_button_short.clicked.connect(self.select_file_short)":
        "- measured standard files; later issue",
    "Windows/CustomSweepWindow.py: self.sweep_button.clicked.connect(self.start_sweep)":
        "sweep_segmented",
    "Windows/DeviceSettings.py: self.btnCaptureScreenshot.clicked.connect(self.captureScreenshot)":
        "device_screenshot",
    "Windows/DeviceSettings.py: self.btnLiveView.clicked.connect(self.liveView)":
        "- continuous video view; device_screenshot covers it",
    "Windows/DeviceSettings.py: self.btnRefresh.clicked.connect(self.updateFields)":
        "device_info",
    "Windows/DisplaySettings.py: btn_add_marker.clicked.connect(self.addMarker)":
        "marker_add",
    "Windows/DisplaySettings.py: btn_add_vswr_marker.clicked.connect(self.addVSWRMarker)":
        "- chart decoration; data_summary gives VSWR ranges",
    "Windows/DisplaySettings.py: btn_marker_settings.clicked.connect(self.displayMarkerWindow)":
        "- marker label layout",
    "Windows/DisplaySettings.py: btn_remove_vswr_marker.clicked.connect(self.removeVSWRMarker)":
        "- chart decoration",
    "Windows/DisplaySettings.py: cp.clicked.connect(self.setColor)":
        "- colours",
    "Windows/DisplaySettings.py: self.btn_manage_bands.clicked.connect(self.displayBandsWindow)":
        "- opens a window; band_list",
    "Windows/DisplaySettings.py: self.btn_remove_marker.clicked.connect(self.removeMarker)":
        "marker_remove",
    "Windows/DisplaySettings.py: self.marker_at_tip.toggled.connect(self.changeMarkerAtTip)":
        "- chart decoration",
    "Windows/DisplaySettings.py: self.returnloss_is_positive.toggled.connect(self.changeReturnLoss)":
        "- display sign convention; actions return return_loss_db > 0",
    "Windows/Files.py: btn_export_file.clicked.connect(lambda: self.exportFile(1))":
        "file_export_touchstone",
    "Windows/Files.py: btn_export_file.clicked.connect(lambda: self.exportFile(4))":
        "file_export_touchstone",
    "Windows/Files.py: btn_load_reference.clicked.connect(self.loadReferenceFile)":
        "file_load_reference",
    "Windows/Files.py: btn_load_sweep.clicked.connect(self.loadSweepFile)":
        "file_load_sweep",
    "Windows/Files.py: btn_open_file_window.clicked.connect(":
        "- opens the file browser of the OS",
    "Windows/MarkerSettings.py: btn_apply.clicked.connect(self.applyButtonClick)":
        "- marker label layout",
    "Windows/MarkerSettings.py: btn_cancel.clicked.connect(self.cancelButtonClick)":
        "- marker label layout",
    "Windows/MarkerSettings.py: btn_default.clicked.connect(self.defaultButtonClick)":
        "- marker label layout",
    "Windows/MarkerSettings.py: btn_ok.clicked.connect(self.okButtonClick)":
        "- marker label layout",
    "Windows/Screenshot.py: self.action_2x_size.triggered.connect(lambda: self.setScale(2))":
        "- view scaling",
    "Windows/Screenshot.py: self.action_3x_size.triggered.connect(lambda: self.setScale(3))":
        "- view scaling",
    "Windows/Screenshot.py: self.action_4x_size.triggered.connect(lambda: self.setScale(4))":
        "- view scaling",
    "Windows/Screenshot.py: self.action_5x_size.triggered.connect(lambda: self.setScale(5))":
        "- view scaling",
    "Windows/Screenshot.py: self.action_original_size.triggered.connect(lambda: self.setScale(1))":
        "- view scaling",
    "Windows/Screenshot.py: self.action_save_screenshot.triggered.connect(self.saveScreenshot)":
        "device_screenshot",
    "Windows/SweepSettings.py: btn_set_band_sweep.clicked.connect(lambda: self.update_band(True))":
        "sweep_set_band",
    "Windows/SweepSettings.py: checkbox.toggled.connect(":
        "sweep_configure",
    "Windows/SweepSettings.py: radio_button.clicked.connect(":
        "sweep_configure",
    "Windows/SweepSettings.py: radio_button.clicked.connect(lambda: self.update_mode(SweepMode.SINGLE))":
        "sweep_configure",
    "Windows/SweepSettings.py: radio_button.clicked.connect(partial(self.update_padding, value))":
        "sweep_set_band",
}
