#!/usr/bin/gjs -m
// Stand-alone window for the animation settings.
import Adw from 'gi://Adw?version=1';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Gtk from 'gi://Gtk?version=4.0';

const here = Gio.File.new_for_uri(import.meta.url).get_parent();
const {buildPage} = await import(here.get_child('studio.js').get_uri());

function settings() {
    const source = Gio.SettingsSchemaSource.new_from_directory(
        here.get_child('schemas').get_path(), Gio.SettingsSchemaSource.get_default(), false);
    return new Gio.Settings({
        settings_schema: source.lookup('org.gnome.shell.extensions.peekaboo', true),
    });
}

GLib.set_application_name('Peekaboo Studio');
const app = new Adw.Application({application_id: 'io.github.hesam_deb.Peekaboo.Studio'});
app.connect('activate', () => {
    let window = app.active_window;
    if (!window) {
        window = new Adw.ApplicationWindow({
            application: app, title: 'Peekaboo Studio', default_width: 620, default_height: 820,
            icon_name: 'io.github.hesam_deb.Peekaboo',
        });
        const view = new Adw.ToolbarView({content: buildPage(settings())});
        view.add_top_bar(new Adw.HeaderBar());
        window.content = view;
    }
    window.present();
});
await app.runAsync([]);
