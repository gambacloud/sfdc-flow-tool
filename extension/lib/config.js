// One place for deployment-specific values.
//
// BACKEND_URL is the hosted backend: the flow tool mounted at /flow-tool on the
// salesforce-debugtool Heroku app (no trailing slash). manifest.json host_permissions
// and content_security_policy.connect-src carry its origin; `python
// tools/check.py` fails if they disagree.
export const BACKEND_URL = "https://salesforce-debugtool.herokuapp.com/flow-tool";

// Hosts the extension may read a Salesforce session for. Mirrors the
// host_permissions entries in manifest.json.
export const SALESFORCE_SUFFIXES = ["salesforce.com", "force.com", "salesforce-setup.com"];

// Salesforce API version used by the (future) org client.
export const API_VERSION = "64.0";
