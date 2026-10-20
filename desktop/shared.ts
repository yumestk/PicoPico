export interface HostInfo {
  protocol: string;
  workspace: string;
  instance_id: string;
  status: string;
  backend_version?: string;
  api_version?: number;
  capabilities?: string[];
}

export interface Workspace {
  handle: string;
  workspace: string;
  info: HostInfo;
}

export interface DesktopAPI {
  chooseWorkspace(): Promise<Workspace | null>;
  refreshWorkspace(handle: string): Promise<Workspace>;
}
