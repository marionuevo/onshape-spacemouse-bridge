"""Bridges spacenavd (a 3Dconnexion SpaceMouse on Linux) to Onshape running
in a browser.

Onshape does not use WebHID. Its 3Dconnexion integration is a client library
(3dconnexion.js) that talks WAMP v1 over a secure WebSocket to a fixed local
address, 127.51.68.120:8181 -- normally served by 3Dconnexion's own Windows
or macOS driver. That driver has no Linux build, so this package plays its
part: it reads 6-DoF input from spacenavd, computes camera motion the way the
real driver would, and speaks the same protocol back to the page.

See docs/ in this repo for the reverse-engineered protocol details this is
built from.
"""
