// One place for deployment-specific values.
//
// BACKEND_URL is a PLACEHOLDER. Replace it with the hosted backend origin
// (no trailing slash) and make the same change to host_permissions and
// content_security_policy.connect-src in manifest.json. `python
// tools/check.py` fails if the three disagree.
export const BACKEND_URL = "https://backend.example.com";

// Hosts the extension may read a Salesforce session for. Mirrors the
// host_permissions entries in manifest.json.
export const SALESFORCE_SUFFIXES = ["salesforce.com", "force.com", "salesforce-setup.com"];

// Salesforce API version used by the (future) org client.
export const API_VERSION = "64.0";
