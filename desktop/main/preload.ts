import { contextBridge, ipcRenderer } from 'electron';
import type { DesktopAPI } from '../shared';

const api: DesktopAPI = {
  chooseWorkspace: () => ipcRenderer.invoke('workspace:choose'),
  refreshWorkspace: handle => ipcRenderer.invoke('workspace:refresh', handle),
};
contextBridge.exposeInMainWorld('pico', api);
