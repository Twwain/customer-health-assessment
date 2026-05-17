import axios from "axios";

const api = axios.create({ baseURL: "/api" });

export interface Customer {
  id: number;
  customer_name: string;
  industry: string;
  contact_person: string;
  contact_phone: string;
  cooperation_years: number;
  contact_frequency: string;
  last_contact_date: string | null;
  customer_satisfaction: number;
  contract_amount: number;
  payment_status: string;
  risk_signals: string;
  competitor_involvement: boolean;
  growth_potential: string;
  notes: string;
  custom_fields: Record<string, string>;
  created_at: string;
  updated_at: string;
}

export interface CustomerListResponse {
  items: Customer[];
  total: number;
  page: number;
  page_size: number;
}

export interface DimensionScore {
  name: string;
  score: number;
  max_score: number;
  details: string[];
}

export interface Assessment {
  customer_id: number;
  customer_name: string;
  total_score: number;
  level: string;
  level_color: string;
  dimensions: DimensionScore[];
  risk_alerts: string[];
  suggestions: string[];
  assessed_at: string;
}

export interface CustomerHealthSummary {
  customer_id: number;
  customer_name: string;
  industry: string;
  total_score: number;
  level: string;
  level_color: string;
}

export interface Overview {
  total_customers: number;
  avg_score: number;
  risk_count: number;
  level_distribution: Record<string, number>;
  recent_customers: CustomerHealthSummary[];
  risk_customers: CustomerHealthSummary[];
}

export function listCustomers(params?: {
  search?: string;
  industry?: string;
  level?: string;
  page?: number;
  page_size?: number;
}) {
  return api.get<CustomerListResponse>("/customers", { params });
}

export function getCustomer(id: number) {
  return api.get<Customer>(`/customers/${id}`);
}

export function createCustomer(data: Partial<Customer>) {
  return api.post<Customer>("/customers", data);
}

export function updateCustomer(id: number, data: Partial<Customer>) {
  return api.put<Customer>(`/customers/${id}`, data);
}

export function deleteCustomer(id: number) {
  return api.delete(`/customers/${id}`);
}

export function importCustomers(file: File) {
  const form = new FormData();
  form.append("file", file);
  return api.post("/customers/import", form);
}

export function getAssessment(id: number) {
  return api.get<Assessment>(`/assessment/${id}`);
}

export function getPdfUrl(id: number) {
  return `/api/assessment/${id}/pdf`;
}

export function getOverview() {
  return api.get<Overview>("/assessment/all/overview");
}

export function listIndustries() {
  return api.get<string[]>("/customers/industries");
}
