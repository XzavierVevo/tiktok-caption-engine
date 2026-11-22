/**
 * Show an error to the user with improved messaging for known issues.
 * Handles storage unavailable errors with a friendlier message.
 * @param {Error|string} err - The error to display
 * @param {Object} opts - Options: {toast: boolean} to use showToast instead of alert
 */
function showUserError(err, opts = {}) {
  const msg = err && err.message ? err.message : String(err || "Unknown error");
  const lower = msg.toLowerCase();
  let displayMsg = msg;
  // Check for storage unavailable (503) - suggest retry
  if (lower.includes("storage") && lower.includes("unavailable")) {
    displayMsg = "Cloud storage is temporarily unavailable. Please wait a moment and try again.\n\nIf this persists, check that your worker is running.";
  }
  if (opts.toast && typeof showToast === "function") {
    showToast(displayMsg, { error: true });
  } else {
    alert(displayMsg);
  }
}
window.showUserError = showUserError;
