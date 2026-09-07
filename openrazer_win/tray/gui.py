"""A small Tk control panel for openrazer-win.

Deliberately stdlib-only: Tk ships with the python.org and Microsoft Store
builds of Python, so the GUI works from a plain ``pip install`` with no extra
wheels and no compiler.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import colorchooser, messagebox, ttk
from typing import Optional

from .._version import UPSTREAM_VERSION, __version__
from ..client import DaemonUnavailable, DeviceManager
from ..client.rpc import is_daemon_running
from ..daemon.protocol import RpcError
from ..devices.recipes import NotSupported

WINDOW_TITLE = 'OpenRazer for Windows'

#: Effects that take a colour, and how many.
COLOUR_COUNTS = {
    'static': 1, 'blinking': 1, 'reactive': 1, 'breath_single': 1,
    'breath_dual': 2, 'breath_triple': 3, 'starlight_single': 1,
    'starlight_dual': 2, 'ripple': 1, 'custom': 1,
}

#: Effects that take a speed.
SPEED_EFFECTS = frozenset((
    'reactive', 'starlight_random', 'starlight_single', 'starlight_dual'))

#: Effects that take a direction.
DIRECTION_EFFECTS = frozenset(('wave', 'wheel'))

DEFAULT_COLOURS = ('#00ff00', '#0000ff', '#ff0000')


class ControlPanel(ttk.Frame):
    """Device list on the left, per-device controls on the right."""

    def __init__(self, master: tk.Misc, manager: Optional[DeviceManager] = None):
        super().__init__(master, padding=10)
        self.manager = manager
        self.devices: list = []
        self.current = None
        self._results: queue.Queue = queue.Queue()
        self.colours = list(DEFAULT_COLOURS)

        self.grid(row=0, column=0, sticky='nsew')
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        self._build_device_list()
        self._build_controls()
        self._build_status_bar()
        self.after(100, self._drain_results)

    # -- layout ------------------------------------------------------------
    def _build_device_list(self) -> None:
        frame = ttk.LabelFrame(self, text='Devices', padding=6)
        frame.grid(row=0, column=0, sticky='nsw', padx=(0, 10))
        frame.rowconfigure(0, weight=1)

        self.device_list = tk.Listbox(frame, width=30, height=12,
                                      exportselection=False, activestyle='none')
        self.device_list.grid(row=0, column=0, sticky='nsew')
        self.device_list.bind('<<ListboxSelect>>', self._on_device_selected)

        ttk.Button(frame, text='Rescan', command=self.reload).grid(
            row=1, column=0, sticky='ew', pady=(6, 0))

    def _build_controls(self) -> None:
        frame = ttk.Frame(self)
        frame.grid(row=0, column=1, sticky='nsew')
        frame.columnconfigure(0, weight=1)

        self.device_title = ttk.Label(frame, text='No device selected',
                                      font=('Segoe UI', 12, 'bold'))
        self.device_title.grid(row=0, column=0, sticky='w')
        self.device_detail = ttk.Label(frame, text='', foreground='#666666')
        self.device_detail.grid(row=1, column=0, sticky='w', pady=(0, 8))

        lighting = ttk.LabelFrame(frame, text='Lighting', padding=8)
        lighting.grid(row=2, column=0, sticky='ew')
        lighting.columnconfigure(1, weight=1)

        ttk.Label(lighting, text='Zone').grid(row=0, column=0, sticky='w')
        self.zone_var = tk.StringVar()
        self.zone_box = ttk.Combobox(lighting, textvariable=self.zone_var,
                                     state='readonly', width=18)
        self.zone_box.grid(row=0, column=1, sticky='w', pady=2)
        self.zone_box.bind('<<ComboboxSelected>>', lambda _e: self._refresh_effects())

        ttk.Label(lighting, text='Effect').grid(row=1, column=0, sticky='w')
        self.effect_var = tk.StringVar()
        self.effect_box = ttk.Combobox(lighting, textvariable=self.effect_var,
                                       state='readonly', width=18)
        self.effect_box.grid(row=1, column=1, sticky='w', pady=2)
        self.effect_box.bind('<<ComboboxSelected>>', lambda _e: self._refresh_options())

        self.colour_frame = ttk.Frame(lighting)
        self.colour_frame.grid(row=2, column=0, columnspan=2, sticky='w', pady=4)
        self.colour_buttons = []
        for index in range(3):
            button = tk.Button(self.colour_frame, width=4, relief='groove',
                               command=lambda i=index: self._choose_colour(i))
            button.grid(row=0, column=index, padx=(0, 4))
            self.colour_buttons.append(button)
        self._paint_colour_buttons()

        self.speed_label = ttk.Label(lighting, text='Speed')
        self.speed_var = tk.IntVar(value=1)
        self.speed_box = ttk.Combobox(lighting, textvariable=self.speed_var,
                                      state='readonly', width=5,
                                      values=(1, 2, 3, 4))
        self.direction_label = ttk.Label(lighting, text='Direction')
        self.direction_var = tk.StringVar(value='right')
        self.direction_box = ttk.Combobox(lighting, textvariable=self.direction_var,
                                          state='readonly', width=8,
                                          values=('left', 'right'))

        ttk.Button(lighting, text='Apply effect', command=self.apply_effect).grid(
            row=5, column=0, columnspan=2, sticky='w', pady=(8, 0))

        ttk.Label(lighting, text='Brightness').grid(row=6, column=0, sticky='w',
                                                    pady=(10, 0))
        self.brightness_var = tk.DoubleVar(value=75.0)
        self.brightness_scale = ttk.Scale(
            lighting, from_=0, to=100, orient='horizontal',
            variable=self.brightness_var, command=self._on_brightness_preview)
        self.brightness_scale.grid(row=6, column=1, sticky='ew', pady=(10, 0))
        self.brightness_scale.bind('<ButtonRelease-1>', self._on_brightness_commit)
        self.brightness_value = ttk.Label(lighting, text='75%')
        self.brightness_value.grid(row=7, column=1, sticky='w')

        self.performance = ttk.LabelFrame(frame, text='Performance', padding=8)
        self.performance.grid(row=3, column=0, sticky='ew', pady=(10, 0))
        self.performance.columnconfigure(1, weight=1)

        ttk.Label(self.performance, text='DPI').grid(row=0, column=0, sticky='w')
        self.dpi_var = tk.IntVar(value=1800)
        self.dpi_scale = ttk.Scale(self.performance, from_=100, to=16000,
                                   orient='horizontal', variable=self.dpi_var,
                                   command=self._on_dpi_preview)
        self.dpi_scale.grid(row=0, column=1, sticky='ew')
        self.dpi_scale.bind('<ButtonRelease-1>', self._on_dpi_commit)
        self.dpi_value = ttk.Label(self.performance, text='1800')
        self.dpi_value.grid(row=1, column=1, sticky='w')

        ttk.Label(self.performance, text='Poll rate').grid(row=2, column=0, sticky='w')
        self.poll_var = tk.StringVar()
        self.poll_box = ttk.Combobox(self.performance, textvariable=self.poll_var,
                                     state='readonly', width=8)
        self.poll_box.grid(row=2, column=1, sticky='w', pady=(4, 0))
        self.poll_box.bind('<<ComboboxSelected>>', self._on_poll_rate)

    def _build_status_bar(self) -> None:
        self.status_var = tk.StringVar(
            value='openrazer-win {0}  (device data from openrazer {1})'.format(
                __version__, UPSTREAM_VERSION))
        ttk.Label(self, textvariable=self.status_var, foreground='#444444',
                  anchor='w').grid(row=1, column=0, columnspan=2, sticky='ew',
                                   pady=(10, 0))

    # -- data --------------------------------------------------------------
    def reload(self) -> None:
        try:
            if self.manager is None:
                self.manager = DeviceManager()
            self.devices = self.manager.refresh()
        except DaemonUnavailable as error:
            self.status_var.set(str(error))
            self.devices = []
        self.device_list.delete(0, tk.END)
        for device in self.devices:
            self.device_list.insert(tk.END, device.name)
        if self.devices:
            self.device_list.selection_set(0)
            self._select(0)
        else:
            self.device_title.config(text='No devices found')
            self.device_detail.config(text='Is the daemon running?')

    def _on_device_selected(self, _event) -> None:
        selection = self.device_list.curselection()
        if selection:
            self._select(selection[0])

    def _select(self, index: int) -> None:
        self.current = self.devices[index]
        device = self.current
        self.device_title.config(text=device.name)
        self.device_detail.config(text='{0}  |  serial {1}  |  firmware {2}'.format(
            device.type, device.serial, device.firmware_version or '?'))

        zones = device.zones
        self.zone_box.config(values=zones)
        if zones:
            self.zone_var.set(device.primary_zone())
        self._refresh_effects()

        capabilities = device.capabilities
        has_dpi = bool(capabilities.get('dpi'))
        has_poll = bool(capabilities.get('poll_rate'))
        if has_dpi or has_poll:
            self.performance.grid()
        else:
            self.performance.grid_remove()
        if has_dpi:
            self.dpi_scale.config(to=capabilities.get('max_dpi') or 16000)
            self._run(lambda: device.dpi, self._apply_dpi_readback)
        if has_poll:
            self.poll_box.config(values=[str(r) for r in device.supported_poll_rates])
            self._run(lambda: device.poll_rate,
                      lambda value: self.poll_var.set(str(value)))
        self._run(lambda: device._call('get_brightness', zone=self.zone_var.get()),
                  self._apply_brightness_readback)

    def _refresh_effects(self) -> None:
        if self.current is None:
            return
        zone = self.zone_var.get()
        effects = self.current.capabilities.get('zones', {}).get(
            zone, {}).get('effects', [])
        if self.current.capabilities.get('software_effects'):
            effects = list(effects) + ['ripple', 'ripple_random']
        self.effect_box.config(values=effects)
        if effects and self.effect_var.get() not in effects:
            self.effect_var.set(effects[0])
        self._refresh_options()

    def _refresh_options(self) -> None:
        effect = self.effect_var.get()
        needed = COLOUR_COUNTS.get(effect, 0)
        for index, button in enumerate(self.colour_buttons):
            if index < needed:
                button.grid()
            else:
                button.grid_remove()
        if effect in SPEED_EFFECTS:
            self.speed_label.grid(row=3, column=0, sticky='w')
            self.speed_box.grid(row=3, column=1, sticky='w')
        else:
            self.speed_label.grid_remove()
            self.speed_box.grid_remove()
        if effect in DIRECTION_EFFECTS:
            self.direction_label.grid(row=4, column=0, sticky='w')
            self.direction_box.grid(row=4, column=1, sticky='w')
        else:
            self.direction_label.grid_remove()
            self.direction_box.grid_remove()

    # -- actions -----------------------------------------------------------
    def _choose_colour(self, index: int) -> None:
        chosen = colorchooser.askcolor(color=self.colours[index], parent=self)
        if chosen and chosen[1]:
            self.colours[index] = chosen[1]
            self._paint_colour_buttons()

    def _paint_colour_buttons(self) -> None:
        for button, colour in zip(self.colour_buttons, self.colours):
            button.configure(background=colour, activebackground=colour)

    def _rgb(self, index: int) -> tuple:
        value = self.colours[index].lstrip('#')
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))

    def apply_effect(self) -> None:
        if self.current is None:
            return
        device = self.current
        effect = self.effect_var.get()
        zone = self.zone_var.get()
        fx = device.fx_for(zone)
        first, second, third = self._rgb(0), self._rgb(1), self._rgb(2)
        speed = int(self.speed_var.get())
        direction = 1 if self.direction_var.get() == 'left' else 2

        def action():
            if effect == 'none':
                fx.none()
            elif effect == 'on':
                fx.on()
            elif effect == 'static':
                fx.static(*first)
            elif effect == 'spectrum':
                fx.spectrum()
            elif effect == 'wave':
                fx.wave(direction)
            elif effect == 'wheel':
                fx.wheel(direction)
            elif effect == 'reactive':
                fx.reactive(*first, speed=speed)
            elif effect == 'blinking':
                fx.blinking(*first)
            elif effect == 'breath_random':
                fx.breath_random()
            elif effect == 'breath_single':
                fx.breath_single(*first)
            elif effect == 'breath_dual':
                fx.breath_dual(*first, *second)
            elif effect == 'breath_triple':
                fx.breath_triple(*first, *second, *third)
            elif effect == 'starlight_random':
                fx.starlight_random(speed)
            elif effect == 'starlight_single':
                fx.starlight_single(*first, speed=speed)
            elif effect == 'starlight_dual':
                fx.starlight_dual(*first, *second, speed=speed)
            elif effect == 'ripple':
                fx.ripple(*first)
            elif effect == 'ripple_random':
                fx.ripple_random()
            elif effect == 'custom':
                fx.advanced.fill(first).draw()
            return '{0}: {1} on {2}'.format(device.name, effect, zone)

        self._run(action, self.status_var.set)

    def _on_brightness_preview(self, _value) -> None:
        self.brightness_value.config(text='{0:.0f}%'.format(self.brightness_var.get()))

    def _on_brightness_commit(self, _event) -> None:
        if self.current is None:
            return
        device, zone = self.current, self.zone_var.get()
        value = round(self.brightness_var.get(), 1)
        self._run(lambda: device._call('set_brightness', value, zone=zone),
                  lambda _r: self.status_var.set(
                      '{0}: brightness {1}%'.format(device.name, value)))

    def _apply_brightness_readback(self, value) -> None:
        try:
            self.brightness_var.set(float(value))
            self.brightness_value.config(text='{0:.0f}%'.format(float(value)))
        except (TypeError, ValueError):
            pass

    def _on_dpi_preview(self, _value) -> None:
        self.dpi_value.config(text='{0:.0f}'.format(self.dpi_var.get()))

    def _on_dpi_commit(self, _event) -> None:
        if self.current is None:
            return
        device = self.current
        value = int(self.dpi_var.get())
        self._run(lambda: setattr(device, 'dpi', (value, value)),
                  lambda _r: self.status_var.set(
                      '{0}: DPI {1}'.format(device.name, value)))

    def _apply_dpi_readback(self, value) -> None:
        try:
            self.dpi_var.set(int(value[0]))
            self.dpi_value.config(text=str(int(value[0])))
        except (TypeError, ValueError, IndexError):
            pass

    def _on_poll_rate(self, _event) -> None:
        if self.current is None:
            return
        device = self.current
        rate = int(self.poll_var.get())
        self._run(lambda: setattr(device, 'poll_rate', rate),
                  lambda _r: self.status_var.set(
                      '{0}: {1} Hz'.format(device.name, rate)))

    # -- background work ---------------------------------------------------
    def _run(self, action, on_success=None) -> None:
        """Run a device call off the UI thread so the window never freezes."""
        def worker():
            try:
                self._results.put((on_success, action(), None))
            except (RpcError, NotSupported, DaemonUnavailable) as error:
                self._results.put((None, None, error))
            except Exception as error:  # noqa: BLE001 - surfaced in the status bar
                self._results.put((None, None, error))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_results(self) -> None:
        while True:
            try:
                callback, result, error = self._results.get_nowait()
            except queue.Empty:
                break
            if error is not None:
                self.status_var.set(str(error))
            elif callback is not None:
                try:
                    callback(result)
                except Exception:  # noqa: BLE001 - UI callbacks must not crash
                    pass
        self.after(100, self._drain_results)


def run(manager: Optional[DeviceManager] = None) -> int:
    root = tk.Tk()
    root.title(WINDOW_TITLE)
    root.minsize(720, 460)
    try:
        style = ttk.Style(root)
        if 'vista' in style.theme_names():
            style.theme_use('vista')
    except tk.TclError:
        pass

    if manager is None and not is_daemon_running():
        _offer_to_start_the_daemon(root)

    panel = ControlPanel(root, manager)
    panel.reload()
    root.mainloop()
    return 0


def _offer_to_start_the_daemon(root: tk.Misc) -> None:
    """Ask once, start once, and say what happened.

    The panel is useless without the daemon, but it must never end up asking
    again in a loop -- which is what happened when the frozen GUI relaunched
    itself and the new process opened a second panel instead of a daemon.
    """
    if not messagebox.askyesno(
            WINDOW_TITLE,
            'The openrazer-win daemon is not running.\n\n'
            'It owns the connection to your devices. Start it now?'):
        return

    from ..cli import start_daemon_detached
    from ..daemon.logs import log_path

    root.config(cursor='watch')
    root.update_idletasks()
    try:
        started = start_daemon_detached()
    finally:
        root.config(cursor='')

    if not started:
        messagebox.showerror(
            WINDOW_TITLE,
            'The daemon did not start.\n\n'
            'Its log may say why:\n{0}\n\n'
            'You can also run "openrazer-win daemon run" in a terminal to see '
            'the error directly.'.format(log_path()))


def main(argv=None) -> int:
    return run()


if __name__ == '__main__':
    raise SystemExit(main())
