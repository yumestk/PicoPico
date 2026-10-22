import { contextBridge, ipcRenderer } from 'electron';
import type { Activity, DesktopAPI } from '../shared';

const api: DesktopAPI = {
  modelSettings: () => ipcRenderer.invoke('models:get'),
  saveModelSettings: value => ipcRenderer.invoke('models:save', value),
  importModelSettings: () => ipcRenderer.invoke('models:import'),
  chooseWorkspace: () => ipcRenderer.invoke('workspace:choose'),
  recentWorkspaces: () => ipcRenderer.invoke('workspace:recent'),
  openRecent: id => ipcRenderer.invoke('workspace:openRecent', id),
  restoreWorkspace: () => ipcRenderer.invoke('workspace:restore'),
  refreshWorkspace: handle => ipcRenderer.invoke('workspace:refresh', handle),
  upgradeHost: handle => ipcRenderer.invoke('workspace:upgrade', handle),
  rememberSelection: (handle, selection) => ipcRenderer.invoke('workspace:selection', handle, selection),
  request: (handle, method, params) => ipcRenderer.invoke('host:request', handle, method, params),
  watchRun: (handle, subscriptionId, runId, after) => ipcRenderer.invoke('host:watch', handle, subscriptionId, runId, after),
  stopWatch: id => ipcRenderer.invoke('host:unwatch', id),
  onActivity: callback => {
    const listener = (_event: Electron.IpcRendererEvent, activity: Activity) => callback(activity);
    ipcRenderer.on('host:activity', listener);
    return () => { ipcRenderer.removeListener('host:activity', listener); };
  },
};
contextBridge.exposeInMainWorld('pico', api);
