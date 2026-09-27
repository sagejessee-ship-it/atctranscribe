"""Native worker: observes source files and reports to the control plane.

The worker never imports the database layer (enforced by tests). Everything it
learns goes through the HTTP API.
"""
