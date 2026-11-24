import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

import {buildPage} from './studio.js';

export default class PeekabooPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        window.set_default_size(620, 820);
        window.add(buildPage(this.getSettings()));
    }
}
