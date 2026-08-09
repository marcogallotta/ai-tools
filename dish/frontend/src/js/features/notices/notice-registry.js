export const noticeRegistry = Object.freeze({
  isolated: { label: "ISOLATED", severity: "warning" },
  lease_attention: { label: "Lease needs attention", severity: "warning" },
  verification_attention: { label: "PENDING REVIEW", severity: "warning" },
  hold_active: { label: "On hold", severity: "warning" },
  recovery_required: { label: "Recovery required", severity: "error" },
  abandonment_active: { label: "Abandonment active", severity: "error" },
  succession_active: { label: "Succession active", severity: "error" },
  projection_abnormal: { label: "Asana projection issue", severity: "warning" },
  render_rejected: { label: "Task content shown as inert plain text", severity: "warning" },
  initial_load_failed: { label: "Board unavailable", severity: "error" },
  service_unavailable: { label: "Refresh unavailable", severity: "warning" },
});
