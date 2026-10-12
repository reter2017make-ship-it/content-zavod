"""Instagram AutoPoster: publishes ready Reels through Instagram Web in a separate Chrome profile (Playwright).

An isolated module: the factory does not import it unless `instagram_web.enabled` is on, and removing this
package (plus its router line in app/web/server.py) leaves the rest of the factory working.
Nothing here types passwords, solves CAPTCHAs or works around Instagram checks: it stops and waits for a person.
"""
