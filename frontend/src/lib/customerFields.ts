/** 客户基本信息字段（与后端 CustomerUpdate 一致），与客情因子区分开。 */
export const BASIC_FIELDS = [
  "customer_name",
  "industry",
  "contact_person",
  "contact_phone",
  "notes",
] as const;
