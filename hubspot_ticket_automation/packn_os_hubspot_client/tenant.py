"""Tenant constant - mirrors src/lib/tenant.ts.

v1 hardcodes 'packn' for the single Pack'N tenant. When productization lands
(future PROD-V2-* requirements), this constant will be replaced by an
env-injected tenant id so the helper can serve any 3PL.
"""

TENANT_ID = "packn"
