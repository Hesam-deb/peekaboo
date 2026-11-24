CFLAGS ?= -O2 -g
CFLAGS += -Wall -Wextra -fPIC -fstack-protector-strong -D_FORTIFY_SOURCE=2
LDFLAGS += -shared -Wl,-z,relro,-z,now
DESTDIR ?=
VERSION := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' src/peekaboo/__init__.py)
MULTIARCH := $(shell $(CC) -print-multiarch)
# Debian and Ubuntu, Fedora, Arch.
PAMDIR ?= $(firstword $(wildcard /usr/lib/$(MULTIARCH)/security /usr/lib64/security /usr/lib/security))
APP := io.github.hesam_deb.Peekaboo
EXT := /usr/share/gnome-shell/extensions/peekaboo@hesam-deb.github.io
ICONS := /usr/share/icons/hicolor

.PHONY: all models test install deb site clean
all: build/pam_peekaboo.so

build/pam_peekaboo.so: pam/pam_peekaboo.c
	mkdir -p build
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $< -lpam

models:
	PYTHONPATH=src python3 -P tools/fetch_models.py models

test:
	PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -P -m unittest discover -s tests

# The web page, with the animations straight from the extension.
site:
	rm -rf build/site
	install -D -m644 -t build/site site/* extension/glyphs.js
	install -D -m644 -t build/site/docs docs/icon.svg docs/app.png

install: all
	install -d $(DESTDIR)/usr/lib/peekaboo/peekaboo $(DESTDIR)/usr/share/peekaboo/models $(DESTDIR)/etc/peekaboo
	install -m644 src/peekaboo/*.py $(DESTDIR)/usr/lib/peekaboo/peekaboo/
	install -m755 data/peekabood data/setup $(DESTDIR)/usr/lib/peekaboo/
	install -D -m755 data/peekaboo $(DESTDIR)/usr/bin/peekaboo
	install -m644 models/*.onnx $(DESTDIR)/usr/share/peekaboo/models/
	install -D -m644 build/pam_peekaboo.so $(DESTDIR)$(PAMDIR)/pam_peekaboo.so
	install -D -m644 -t $(DESTDIR)/usr/lib/systemd/system data/peekaboo.socket data/peekaboo.service
	install -D -m644 -t $(DESTDIR)/usr/share/polkit-1/actions data/io.github.hesam-deb.peekaboo.policy
	install -D -m644 -t $(DESTDIR)/usr/share/applications data/$(APP).desktop
	install -D -m644 -t $(DESTDIR)/usr/share/metainfo data/$(APP).metainfo.xml
	install -D -m644 -t $(DESTDIR)$(ICONS)/scalable/apps data/icons/$(APP).svg
	install -D -m644 -t $(DESTDIR)$(ICONS)/symbolic/apps data/icons/$(APP)-symbolic.svg
	install -D -m644 -t $(DESTDIR)$(EXT) extension/metadata.json extension/*.js
	install -D -m644 -t $(DESTDIR)$(EXT)/schemas extension/schemas/*.gschema.xml
	glib-compile-schemas --strict $(DESTDIR)$(EXT)/schemas
	chmod 644 $(DESTDIR)$(EXT)/schemas/gschemas.compiled
	install -D -m644 -t $(DESTDIR)/usr/share/man/man1 data/man/peekaboo.1
	install -D -m644 data/completion.bash $(DESTDIR)/usr/share/bash-completion/completions/peekaboo
	install -D -m644 data/pam-config $(DESTDIR)/usr/share/pam-configs/peekaboo
	install -D -m644 data/pam-config-password $(DESTDIR)/usr/share/pam-configs/peekaboo-password
	# an untouched (all comments) config is refreshed, an edited one is kept
	grep -qs "^[^#[]" $(DESTDIR)/etc/peekaboo/config.toml || install -m644 data/config.toml $(DESTDIR)/etc/peekaboo/config.toml

deb: all
	rm -rf build/deb
	$(MAKE) -s install DESTDIR=$(CURDIR)/build/deb
	install -d build/deb/DEBIAN build/deb/usr/share/doc/peekaboo
	install -m644 LICENSE build/deb/usr/share/doc/peekaboo/copyright
	sed -e 's/@VERSION@/$(VERSION)/' -e "s/@ARCH@/$$(dpkg --print-architecture)/" \
	    -e "s/@SIZE@/$$(du -sk build/deb/usr | cut -f1)/" packaging/control.in > build/deb/DEBIAN/control
	echo /etc/peekaboo/config.toml > build/deb/DEBIAN/conffiles
	install -m755 packaging/postinst packaging/prerm packaging/postrm build/deb/DEBIAN/
	dpkg-deb --root-owner-group -Zxz --build build/deb build/peekaboo_$(VERSION)_$$(dpkg --print-architecture).deb

clean:
	rm -rf build
