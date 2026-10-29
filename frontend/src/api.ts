import type {
  ConfigResponse,
  ProjectSummary,
  ProjectView,
  LibraryCharacter,
  ReferenceImage,
  Shot,
  ShotImage,
} from "./types";

export class ApiError extends Error {
  status: number;
  detail: string;
  headers: Record<string, string>;
  constructor(status: number, detail: string, headers: Record<string, string>) {
    super(detail);
    this.status = status;
    this.detail = detail;
    this.headers = headers;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": init?.body instanceof Blob ? init.body.type || "application/octet-stream" : "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data);
    } catch {
      /* 非 JSON 错误体 */
    }
    const headers: Record<string, string> = {};
    res.headers.forEach((v, k) => (headers[k] = v));
    throw new ApiError(res.status, detail, headers);
  }
  return (await res.json()) as T;
}

export const api = {
  uploadReference: (id: number, file: File, kind: ReferenceImage["kind"], target?: number) =>
    request<ReferenceImage>(`/api/v1/projects/${id}/references?kind=${kind}${target == null ? "" : `&target_index=${target}`}`, { method: "POST", body: file }),
  deleteReference: (id: number, referenceId: string) =>
    request<{ deleted: boolean }>(`/api/v1/projects/${id}/references/${referenceId}`, { method: "DELETE" }),
  editShot: (id: number, index: number, shot: Omit<Shot, "index">) =>
    request(`/api/v1/projects/${id}/shots/${index}`, { method: "PUT", body: JSON.stringify(shot) }),
  regenerateShot: (id: number, index: number, feedback: string) =>
    request<ShotImage>(`/api/v1/projects/${id}/shots/${index}/regenerate`, { method: "POST", body: JSON.stringify({ feedback }) }),
  listCharacters: () => request<{ items: LibraryCharacter[] }>("/api/v1/characters").then(d => d.items),
  saveCharacter: (id: number, index: number) =>
    request<LibraryCharacter>(`/api/v1/projects/${id}/characters/${index}/library`, { method: "POST" }),
  importCharacter: (id: number, characterId: number) =>
    request(`/api/v1/projects/${id}/library-characters/${characterId}`, { method: "POST" }),
  createCharacter: (values: { name: string; personality: string; appearance: string; quirks: string; style: string }) =>
    request<LibraryCharacter>("/api/v1/characters", { method: "POST", body: JSON.stringify(values) }),
  updateCharacter: (id: number, values: { name: string; personality: string; appearance: string; quirks: string; style: string }) =>
    request<LibraryCharacter>(`/api/v1/characters/${id}`, { method: "PUT", body: JSON.stringify(values) }),
  uploadCharacterImage: (id: number, file: File) =>
    request<LibraryCharacter>(`/api/v1/characters/${id}/image`, { method: "POST", body: file }),
  deleteCharacter: (id: number) => request(`/api/v1/characters/${id}`, { method: "DELETE" }),
  listProjects: () =>
    request<{ items: ProjectView[] }>("/api/v1/projects").then((d) => d.items),

  createProject: (topic: string, style: string) =>
    request<ProjectSummary>("/api/v1/projects", {
      method: "POST",
      body: JSON.stringify({ topic, style }),
    }),

  getProject: (id: number) => request<ProjectView>(`/api/v1/projects/${id}`),

  generate: (id: number, startStage?: string) =>
    request<{ run_id: number }>(`/api/v1/projects/${id}/generate`, {
      method: "POST",
      body: JSON.stringify(startStage ? { start_stage: startStage } : {}),
    }),

  confirm: (id: number, runId: number, feedback: string) =>
    request<{ run_id: number; ack: boolean }>(`/api/v1/projects/${id}/confirm`, {
      method: "POST",
      body: JSON.stringify({ run_id: runId, feedback }),
    }),

  resume: (id: number) =>
    request<{ run_id: number; resumed: boolean }>(`/api/v1/projects/${id}/resume`, {
      method: "POST",
      body: JSON.stringify({}),
    }),

  cancel: (id: number) =>
    request<{ cancelled: boolean }>(`/api/v1/projects/${id}/cancel`, { method: "POST" }),

  deleteProject: (id: number) =>
    request<{ deleted: boolean; project_id: number }>(`/api/v1/projects/${id}`, {
      method: "DELETE",
    }),

  getConfig: () => request<ConfigResponse>("/api/v1/config"),

  saveConfig: (values: Record<string, string>) =>
    request<ConfigResponse>("/api/v1/config", {
      method: "PUT",
      body: JSON.stringify({ values }),
    }),
};
