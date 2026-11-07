import Gio from 'gi://Gio';
import Clutter from 'gi://Clutter';
import Meta from 'gi://Meta';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';

const xml = `<node><interface name="com.vidgrab.Validation">
  <method name="Snapshot"><arg type="s" direction="out"/></method>
  <method name="CloseWindow"><arg type="i" direction="in"/></method>
  <method name="FocusWindow"><arg type="i" direction="in"/></method>
</interface></node>`;

function geometry(actor) {
  const [x, y] = actor.get_transformed_position();
  const [width, height] = actor.get_transformed_size();
  return { x, y, width, height };
}

function modalDialogs() {
  return Main.layoutManager.modalDialogGroup.get_children()
    .filter(actor => actor.mapped)
    .map(actor => {
      const labels = [];
      const buttons = [];
      function visit(child) {
        if (!child.mapped)
          return;
        if (child instanceof St.Label && child.text)
          labels.push(child.text);
        if (child instanceof St.Button) {
          const label = child.get_child()?.get_text?.() ?? child.label;
          buttons.push({ label, reactive: child.reactive, ...geometry(child) });
        }
        for (const descendant of child.get_children())
          visit(descendant);
      }
      visit(actor);
      return { labels, buttons };
    });
}

export default class Validation extends Extension {
  enable() {
    this._events = [];
    this._input = global.stage.connect('captured-event', (_stage, event) => {
      if ([Clutter.EventType.BUTTON_PRESS, Clutter.EventType.BUTTON_RELEASE].includes(event.type())) {
        const [x, y] = event.get_coords();
        this._events.push({ pressed: event.type() === Clutter.EventType.BUTTON_PRESS,
          button: event.get_button(), x, y });
        this._events = this._events.slice(-20);
      }
      return Clutter.EVENT_PROPAGATE;
    });
    this._object = Gio.DBusExportedObject.wrapJSObject(xml, this);
    this._object.export(Gio.DBus.session, '/com/vidgrab/Validation');
    this._name = Gio.bus_own_name_on_connection(Gio.DBus.session,
      'com.vidgrab.Validation', Gio.BusNameOwnerFlags.NONE, null, null);
  }

  disable() {
    global.stage.disconnect(this._input);
    Gio.bus_unown_name(this._name);
    this._object.unexport();
    this._object = null;
  }

  Snapshot() {
    const [pointerX, pointerY, modifiers] = global.get_pointer();
    return JSON.stringify({
      wayland: Meta.is_wayland_compositor(),
      overview_visible: Main.overview.visible,
      screen: { width: global.stage.width, height: global.stage.height },
      pointer_events: this._events,
      pointer: { x: pointerX, y: pointerY,
        primary_down: (modifiers & Clutter.ModifierType.BUTTON1_MASK) !== 0 },
      dialogs: modalDialogs(),
      windows: global.get_window_actors().map(actor => {
        const window = actor.meta_window;
        const frame = window.get_frame_rect();
        return { pid: window.get_pid(), title: window.get_title(),
          wayland: window.get_client_type() === Meta.WindowClientType.WAYLAND,
          hidden: window.is_hidden(), visible: actor.visible,
          focused: window.has_focus(),
          frame: { x: frame.x, y: frame.y, width: frame.width, height: frame.height } };
      }),
      indicators: Object.entries(Main.panel.statusArea)
        .filter(([name]) => name.includes('appindicator'))
        .map(([name, item]) => {
          const actor = item.container ?? item;
          return { name, visible: actor.visible, mapped: actor.mapped,
            ...geometry(actor), menu_open: item.menu?.isOpen ?? false,
            menu: item.menu?.isOpen ? item.menu._getMenuItems()
              .filter(entry => entry.label)
              .map(entry => ({ label: entry.label.text, ...geometry(entry) })) : [] };
        }),
    });
  }

  _window(pid) {
    const window = global.get_window_actors().map(actor => actor.meta_window)
      .find(window => window.get_pid() === pid && window.get_title() === 'VidGrab');
    if (!window)
      throw new Error('The selected VidGrab window is unavailable');
    return window;
  }

  CloseWindow(pid) {
    this._window(pid).delete(global.get_current_time());
  }

  FocusWindow(pid) {
    this._window(pid).activate(global.get_current_time());
  }
}
