import axios from "axios";
import type {
  AssessmentResponse,
  AssessmentHistoryResponse,
  AssessmentTrendResponse,
  ChatSessionDetail,
  ChatSessionItem,
  ChatEvent,
  ChatSessionListResponse,
  ChatTurnResponse,
  CustomerListResponse,
  CustomerResponse,
  FactorConfigResponse,
  FactorUpdateResponse,
  KnowledgeItemListResponse,
  KnowledgeItemResponse,
  KnowledgeSearchResponse,
  KnowledgeStatusResponse,
  LLMStatusResponse,
} from "../types";

const http = axios.create({ baseURL: "/api" });

export const customers = {
  list: (params: { search?: string; industry?: string; level?: string; page?: number; page_size?: number } = {}) =>
    http.get<CustomerListResponse>("/customers", { params }).then((r) => r.data),
  get: (id: number) => http.get<CustomerResponse>(`/customers/${id}`).then((r) => r.data),
  create: (data: Partial<CustomerResponse>) =>
    http.post<CustomerResponse>("/customers", data).then((r) => r.data),
  update: (id: number, data: Partial<CustomerResponse>) =>
    http.put<CustomerResponse>(`/customers/${id}`, data).then((r) => r.data),
  updateFactors: (id: number, factors: Record<string, unknown>) =>
    http
      .put<FactorUpdateResponse>(`/customers/${id}/factors`, { factors })
      .then((r) => r.data),
  remove: (id: number) => http.delete(`/customers/${id}`).then((r) => r.data),
  import: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return http.post("/customers/import", form).then((r) => r.data);
  },
  importTemplate: () =>
    http.get<Blob>("/customers/import-template", { responseType: "blob" }).then((r) => r.data),
  pdfJob: (customerId: number, includeAi: boolean) =>
    http
      .post<{ job_id: string; status: string }>(`/assessment/${customerId}/pdf/jobs`, null, {
        params: { include_ai: includeAi },
      })
      .then((r) => r.data),
  pdfJobStatus: (customerId: number, jobId: string) =>
    http
      .get<{ job_id: string; status: string; error?: string }>(
        `/assessment/${customerId}/pdf/jobs/${jobId}`,
      )
      .then((r) => r.data),
  pdfDownload: (customerId: number, jobId: string) =>
    http
      .get<Blob>(`/assessment/${customerId}/pdf/jobs/${jobId}/download`, {
        responseType: "blob",
      })
      .then((r) => r.data),
  industries: () => http.get<string[]>("/customers/industries").then((r) => r.data),
  factorConfig: () =>
    http.get<FactorConfigResponse>("/customers/factor-config").then((r) => r.data),
  assessment: (id: number) =>
    http.get<AssessmentResponse>(`/assessment/${id}`).then((r) => r.data),
  history: (id: number, limit = 50) =>
    http
      .get<AssessmentHistoryResponse>(`/customers/${id}/assessment-history`, { params: { limit } })
      .then((r) => r.data),
  trend: (id: number, limit = 12) =>
    http
      .get<AssessmentTrendResponse>(`/customers/${id}/assessment-trend`, { params: { limit } })
      .then((r) => r.data),
};

export const chat = {
  status: () => http.get<LLMStatusResponse>("/chat/status").then((r) => r.data),
  sessions: (customerId?: number) =>
    http
      .get<ChatSessionListResponse>("/chat/sessions", { params: { customer_id: customerId } })
      .then((r) => r.data),
  createSession: (data: {
    title?: string;
    customer_id?: number;
    scenario?: string;
    system_prompt?: string;
  }) => http.post<ChatSessionItem>("/chat/sessions", data).then((r) => r.data),
  getSession: (id: number) =>
    http.get<ChatSessionDetail>(`/chat/sessions/${id}`).then((r) => r.data),
  deleteSession: (id: number) => http.delete(`/chat/sessions/${id}`).then((r) => r.data),
  // 注：axios 助手按一次性 JSON 返回解析，强制 stream:false；
  // 流式对话请使用下方 streamChat（fetch + SSE）
  send: (id: number, data: { content: string; scenario?: string; customer_id?: number }) =>
    http.post<ChatTurnResponse>(`/chat/sessions/${id}/messages`, { ...data, stream: false }).then((r) => r.data),
  evaluate: (id: number, data: { customer_id?: number }) =>
    http.post<ChatTurnResponse>(`/chat/sessions/${id}/evaluate`, { ...data, stream: false }).then((r) => r.data),
  strategy: (id: number, data: { customer_id?: number }) =>
    http.post<ChatTurnResponse>(`/chat/sessions/${id}/strategy`, { ...data, stream: false }).then((r) => r.data),
  alertAnalysis: (id: number, data: { customer_id?: number }) =>
    http.post<ChatTurnResponse>(`/chat/sessions/${id}/alert-analysis`, { ...data, stream: false }).then((r) => r.data),
  regenerate: (id: number, data: { scenario?: string; customer_id?: number }) =>
    http.post<ChatTurnResponse>(`/chat/sessions/${id}/regenerate`, { ...data, stream: false }).then((r) => r.data),
  feedback: (messageId: number, feedback: string) =>
    http
      .post(`/chat/messages/${messageId}/feedback`, { feedback })
      .then((r) => r.data),
};

export const knowledge = {
  items: (params: { category?: string; status?: string; q?: string; limit?: number } = {}) =>
    http.get<KnowledgeItemListResponse>("/knowledge/items", { params }).then((r) => r.data),
  get: (id: number) => http.get<KnowledgeItemResponse>(`/knowledge/items/${id}`).then((r) => r.data),
  search: (data: { query: string; customer_id?: number; category?: string; status?: string; top_k?: number }) =>
    http.post<KnowledgeSearchResponse>("/knowledge/search", data).then((r) => r.data),
  upload: (file: File, category: string, title?: string) => {
    const form = new FormData();
    form.append("file", file);
    form.append("category", category);
    if (title) form.append("title", title);
    return http.post("/knowledge/upload", form).then((r) => r.data);
  },
  update: (id: number, data: { title?: string; category?: string; tags?: string[] }) =>
    http.put<KnowledgeItemResponse>(`/knowledge/items/${id}`, data).then((r) => r.data),
  remove: (id: number) => http.delete(`/knowledge/items/${id}`).then((r) => r.data),
  approve: (id: number) =>
    http.post<KnowledgeItemResponse>(`/knowledge/items/${id}/approve`).then((r) => r.data),
  reindex: (category?: string) =>
    http.post("/knowledge/reindex", category ? { category } : {}).then((r) => r.data),
  status: () => http.get<KnowledgeStatusResponse>("/knowledge/status").then((r) => r.data),
};

/** 解析 SSE 流：逐 event 回调。兼容后端 stream_sse（event: x / data: json）。 */
export async function streamChat(
  url: string,
  body: unknown,
  onEvent: (ev: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(`流式请求失败：HTTP ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";
    for (const part of parts) {
      const lines = part.split("\n");
      let type = "";
      const dataLines: string[] = [];
      for (const line of lines) {
        if (line.startsWith("event:")) type = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (!type || dataLines.length === 0) continue;
      try {
        onEvent({ type, data: JSON.parse(dataLines.join("\n")) });
      } catch {
        /* 忽略非 JSON 数据 */
      }
    }
  }
}

export default http;
