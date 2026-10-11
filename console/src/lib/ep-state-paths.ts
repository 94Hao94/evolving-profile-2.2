import path from "node:path";
import { homedir } from "node:os";

// Every Console read/write uses the operator's state root. A second root is an
// independent installation, never a way to control the default host's daemons.
export const EP_DEFAULT_STATE_ROOT = path.join(homedir(), ".evolving-profile");
export const EP_STATE_ROOT = path.resolve(process.env.EVOLVING_PROFILE_STATE_ROOT || EP_DEFAULT_STATE_ROOT);
export const epStatePath = (...parts: string[]) => path.join(EP_STATE_ROOT, ...parts);
export const EP_API_ENV = process.env.EVOLVING_PROFILE_API_ENV || epStatePath("profiles/evolving-profile-api.env");
export const EP_MANAGED_MAC_HOST = process.platform === "darwin" && EP_STATE_ROOT === EP_DEFAULT_STATE_ROOT;
export const EP_BACKUP_PLIST = path.join(homedir(), "Library/LaunchAgents/com.evolving-profile.backup.plist");
export const EP_RUNTIME_PYTHON = epStatePath("runtime/python-3.11/bin/python");
export const EP_HOST_SESSIONS = process.env.EVOLVING_PROFILE_HOST_SESSIONS_ROOT || (EP_STATE_ROOT === EP_DEFAULT_STATE_ROOT ? path.join(homedir(), ".codex/sessions") : epStatePath("host-sessions"));
