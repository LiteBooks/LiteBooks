function getCookie(name) {
  const match = document.cookie.split("; ").find((item) => item.startsWith(`${name}=`));
  return match ? decodeURIComponent(match.split("=").slice(1).join("=")) : "";
}

export class ApiError extends Error {
  constructor(message, status, fields = {}) {
    super(message);
    this.status = status;
    this.fields = fields;
  }
}

export async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const method = options.method || "GET";
  let body = options.body;
  if (body && !(body instanceof FormData) && typeof body !== "string") {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(body);
  }
  if (!headers.has("Accept")) headers.set("Accept", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase())) {
    headers.set("X-CSRFToken", getCookie("csrftoken"));
  }
  const response = await fetch(path, { ...options, method, body, headers, credentials: "same-origin" });
  const data = response.headers.get("content-type")?.includes("application/json") ? await response.json() : {};
  if (!response.ok) throw new ApiError(data.error || `Request failed (${response.status}).`, response.status, data.fields || {});
  return data;
}

export function formDataFrom(values, file) {
  const data = new FormData();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== null) data.append(key, typeof value === "object" ? JSON.stringify(value) : value);
  });
  if (file) data.append("attachment", file);
  return data;
}

