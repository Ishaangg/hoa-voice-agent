export type Community = { id: string; name: string; office_phone: string; emergency_policy_summary: string };
export type Case = { id: string; community_id: string; community: string; priority: string; incident_type: string; description: string; status: string; assigned_to?: string | null; assignee_type?: string | null; created_at: string; acknowledged_at?: string | null; resolved_at?: string | null };
export type CostState = { price_status: string; price_shown_to_caller?: string | null; resident_consent: string; quote_status: string; quote_amount_cents?: number | null; quote_approver?: string | null; final_amount_cents?: number | null; billed_to?: string | null };
export type CaseDetail = Case & { resolution_note?: string | null; urgency_rationale: string; cost: CostState; vendor_dispatch?: { vendor_name: string; vendor_phone?: string | null; status: string; notification_channel: string; provider_reference?: string | null; attempt_count: number; failure_detail?: string | null; sent_at?: string | null; acknowledged_at?: string | null; completed_at?: string | null; last_action_at?: string | null } | null; caller: { name?: string | null; phone?: string | null; unit?: string | null; identity_status: string }; transcript: { speaker: string; text: string; created_at: string }[]; timeline: { type: string; detail: string; created_at: string }[]; suggested_routes: { vendor_name: string; phone?: string | null; diagnostic_fee_cents?: number | null; dispatch_mode?: string; payer_responsibility?: string }[]; assignment_options: { name: string; type: string; phone?: string | null }[] };
export type Call = { id: string; community_id: string; community: string; caller_name?: string | null; caller_phone: string; caller_unit?: string | null; state: string; created_at: string; work_order_id?: string | null; work_order_status?: string | null };
export type CallDetail = { id: string; caller: { name?: string | null; phone: string; unit?: string | null; identity_status: string }; transcript: { speaker: string; text: string; created_at: string }[]; work_order_id?: string | null };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: "include", headers: { "Content-Type": "application/json", ...(init?.headers || {}) }, ...init });
  if (!response.ok) throw new Error((await response.json().catch(() => null))?.detail || `Request failed (${response.status})`);
  return response.status === 204 ? (undefined as T) : response.json();
}

export const api = {
  login: (email: string, password: string) => request("/api/v1/console/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),
  logout: () => request<void>("/api/v1/console/auth/logout", { method: "POST" }),
  me: () => request<{ display_name: string; email: string; role: string; management_company_id: string }>("/api/v1/console/me"),
  communities: () => request<Community[]>("/api/v1/console/communities"),
  dashboard: () => request<{ kpis: Record<string, number>; priority_queue: Case[] }>("/api/v1/console/dashboard"),
  cases: () => request<Case[]>("/api/v1/console/cases"),
  calls: () => request<Call[]>("/api/v1/console/calls"),
  callDetail: (id: string) => request<CallDetail>(`/api/v1/console/calls/${id}`),
  caseDetail: (id: string) => request<CaseDetail>(`/api/v1/console/cases/${id}`),
  acknowledge: (id: string) => request(`/api/v1/console/cases/${id}/acknowledge`, { method: "POST" }),
  assign: (id: string, assignee_type: string, assigned_to: string) => request(`/api/v1/console/cases/${id}/assign`, { method: "POST", body: JSON.stringify({ assignee_type, assigned_to }) }),
  resolve: (id: string, resolution_note: string) => request(`/api/v1/console/cases/${id}/resolve`, { method: "POST", body: JSON.stringify({ resolution_note }) }),
  routing: (id: string) => request<{ contacts: { id: string; type: string; name: string; phone?: string; email?: string }[]; vendor_registry: { id: string; name: string; phone?: string }[]; vendor_routes: { id: string; incident_type: string; vendor_id?: string | null; vendor_name: string; phone?: string; diagnostic_fee_cents?: number | null; automatic_approval_limit_cents?: number | null; dispatch_mode: string; payer_responsibility: string }[] }>(`/api/v1/console/communities/${id}/routing`),
  vendors: () => request<{ id: string; name: string; phone?: string; is_active: boolean }[]>("/api/v1/console/vendors"),
  addVendor: (body: { name: string; phone?: string }) => request("/api/v1/console/vendors", { method: "POST", body: JSON.stringify(body) }),
  addRoutingContact: (id: string, body: Record<string, string>) => request(`/api/v1/console/communities/${id}/routing/contacts`, { method: "POST", body: JSON.stringify(body) }),
  addVendorRoute: (id: string, body: Record<string, string>) => request(`/api/v1/console/communities/${id}/routing/vendors`, { method: "POST", body: JSON.stringify(body) }),
  consent: (id: string, status: string) => request(`/api/v1/console/cases/${id}/consent`, { method: "POST", body: JSON.stringify({ status }) }),
  quote: (id: string, amount_cents: number, status: string, approver?: string) => request(`/api/v1/console/cases/${id}/quote`, { method: "POST", body: JSON.stringify({ amount_cents, status, approver }) }),
  billing: (id: string, final_amount_cents: number, billed_to: string) => request(`/api/v1/console/cases/${id}/billing`, { method: "POST", body: JSON.stringify({ final_amount_cents, billed_to }) }),
  knowledge: (id: string) => request<{ id: string; category: string; topic: string; answer: string; source_reference?: string; approval_status: string; updated_at: string }[]>(`/api/v1/console/communities/${id}/knowledge`),
  knowledgeRevisions: (communityId: string, knowledgeId: string) => request<{ action: string; snapshot: string; created_at: string }[]>(`/api/v1/console/communities/${communityId}/knowledge/${knowledgeId}/revisions`),
  saveKnowledge: (id: string, body: Record<string, string>) => request(`/api/v1/console/communities/${id}/knowledge`, { method: "POST", body: JSON.stringify(body) }),
  analytics: () => request<Record<string, number | null>>("/api/v1/console/analytics")
};
