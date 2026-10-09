const TOKEN_KEY = "hub_token";

export class CaptureApiError extends Error {
  constructor(message, status, code, details = {}) {
    super(message);
    this.name = "CaptureApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function fetchJson(path, options = {}, retried = false) {
  const headers = Object.assign({ "X-Actor": "ui" }, options.headers || {});
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) headers.Authorization = "Bearer " + token;
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, Object.assign({}, options, { headers }));

  if (response.status === 401 && !retried) {
    const currentToken = localStorage.getItem(TOKEN_KEY);
    if (currentToken && currentToken !== token) {
      return fetchJson(path, options, true);
    }
    const entered = window.prompt("API token required");
    if (entered) {
      localStorage.setItem(TOKEN_KEY, entered);
      return fetchJson(path, options, true);
    }
  }

  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = body.error || {};
    throw new CaptureApiError(
      error.message || response.statusText || "Request failed",
      response.status,
      error.code || "request_failed",
      error,
    );
  }
  return { body, status: response.status };
}

export async function apiRequest(path, options = {}) {
  return (await fetchJson(path, options)).body;
}

function normalizeCaptureResponse(payload) {
  const raw = payload.capture || payload;
  const capture = Object.assign({}, raw);
  if (raw.id !== undefined && raw.id !== null) capture.id = String(raw.id);
  if (raw.plan && Array.isArray(raw.plan.ops)) {
    capture.plan = Object.assign({}, raw.plan, {
      ops: raw.plan.ops.map((op) => Object.assign({}, op, {
        id: String(op.id),
        op: op.op || op.kind,
      })),
    });
  }
  return capture;
}

function normalizeList(payload) {
  const rows = payload.captures || payload.items || [];
  return {
    captures: rows.map((row) => {
      if (row.plan) return normalizeCaptureResponse(row);
      return Object.assign({}, row, { id: String(row.id) });
    }),
    total: payload.total || 0,
  };
}

function pathFor(id, suffix = "") {
  return "/api/capture/" + encodeURIComponent(String(id)) + suffix;
}

export const captureApi = {
  async config(options) {
    return apiRequest("/api/capture/config", options);
  },
  async list(filters = {}, options) {
    const query = new URLSearchParams();
    for (const key of ["state", "source", "limit", "offset"]) {
      if (filters[key] !== undefined && filters[key] !== "") {
        query.set(key, String(filters[key]));
      }
    }
    const suffix = query.size ? "?" + query.toString() : "";
    return normalizeList(await apiRequest("/api/capture" + suffix, options));
  },
  async create(data, options) {
    const response = await fetchJson("/api/capture", {
      method: "POST",
      body: JSON.stringify(data),
      signal: options && options.signal,
    });
    return {
      status: response.status,
      capture: response.body.capture
        ? normalizeCaptureResponse(response.body)
        : null,
      note: response.body.note || null,
    };
  },
  async createNote(content, options) {
    return apiRequest("/api/notes", {
      method: "POST",
      body: JSON.stringify({ content, source: "capture" }),
      signal: options && options.signal,
    });
  },
  async get(id, options) {
    return normalizeCaptureResponse(await apiRequest(pathFor(id), options));
  },
  async regenerate(id, data = {}, options) {
    return normalizeCaptureResponse(await apiRequest(pathFor(id, "/regenerate"), {
      method: "POST",
      body: JSON.stringify(data),
      signal: options && options.signal,
    }));
  },
  async reject(id, options) {
    return normalizeCaptureResponse(await apiRequest(pathFor(id, "/reject"), {
      method: "POST",
      body: "{}",
      signal: options && options.signal,
    }));
  },
  async saveNote(id, options) {
    return apiRequest(pathFor(id, "/save-note"), {
      method: "POST",
      body: "{}",
      signal: options && options.signal,
    });
  },
  async apply(id, data = {}, options) {
    return apiRequest(pathFor(id, "/apply"), {
      method: "POST",
      body: JSON.stringify(data),
      signal: options && options.signal,
    });
  },
  async undo(id, options) {
    return apiRequest(pathFor(id, "/undo"), {
      method: "POST",
      body: "{}",
      signal: options && options.signal,
    });
  },
  async validate(id, data = {}, options) {
    return apiRequest(pathFor(id, "/validate"), {
      method: "POST",
      body: JSON.stringify(data),
      signal: options && options.signal,
    });
  },
  async getSelf(options) {
    return apiRequest("/api/self", options);
  },
  async putSelf(data, options) {
    return apiRequest("/api/self", {
      method: "PUT",
      body: JSON.stringify(data),
      signal: options && options.signal,
    });
  },
  async entityTypes(options) {
    return apiRequest("/api/entity-types", options);
  },
  async extendType(name, data, options) {
    return apiRequest("/api/entity-types/" + encodeURIComponent(name) + "/extend", {
      method: "POST",
      body: JSON.stringify(data),
      signal: options && options.signal,
    });
  },
  async typeHistory(name, options) {
    return apiRequest(
      "/api/entity-types/" + encodeURIComponent(name) + "/history",
      options,
    );
  },
  async labels(ids, options) {
    return apiRequest("/api/entities/labels", {
      method: "POST",
      body: JSON.stringify({ ids }),
      signal: options && options.signal,
    });
  },
};
