import { useCallback, useEffect, useState } from 'react';
import { ApiError, api, prettyError } from '../api/client';
import type { Capabilities, EngineCapability, GpuDeviceInventory, MaterialCatalogItem, ProjectRow, RunSummary, SpecializedCatalog } from '../api/types';

// Backend reachability plus the catalogs the shell renders before a project is
// opened. `notice` lives here because this hook is the first to write it (a
// failed materials fetch) and App keeps it as the shared toast sink.
export type WorkbenchHealth = 'checking' | 'ready' | 'offline';

export function useWorkbenchBootstrap() {
  const [projectRows, setProjectRows] = useState<ProjectRow[]>([]);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [gpuInventory, setGpuInventory] = useState<GpuDeviceInventory | null>(null);
  const [engineCatalog, setEngineCatalog] = useState<EngineCapability[]>([]);
  const [materials, setMaterials] = useState<MaterialCatalogItem[]>([]);
  const [specializedCatalog, setSpecializedCatalog] = useState<SpecializedCatalog | null>(null);
  const [newSpecializedModel, setNewSpecializedModel] = useState('sf6o2');
  const [health, setHealth] = useState<WorkbenchHealth>('checking');
  const [notice, setNotice] = useState('');

  const refreshLists = useCallback(async () => {
    try {
      const [projectData, runData] = await Promise.all([api<{ projects: ProjectRow[] }>('/api/projects'), api<{ runs: RunSummary[] }>('/api/runs')]);
      if (!Array.isArray(projectData.projects) || !Array.isArray(runData.runs)) throw new ApiError('本机工作台返回的项目或运行记录格式无效。', null);
      setProjectRows(projectData.projects); setRuns(runData.runs);
    } catch (error) { setNotice(prettyError(error)); }
  }, []);

  useEffect(() => {
    let mounted = true;
    void Promise.all([
      api<Record<string, unknown>>('/api/health'),
      api<Capabilities>('/api/capabilities'),
      api<GpuDeviceInventory>('/api/gpu-devices'),
      api<SpecializedCatalog>('/api/specialized-models'),
      api<{ engines: EngineCapability[] }>('/api/engines'),
    ]).then(([, cap, gpuInfo, specialized, engineData]) => {
      if (!Array.isArray(specialized.models)) throw new ApiError('本机工作台返回的专用模型目录格式无效。', null);
      if (!Array.isArray(engineData.engines)) throw new ApiError('本机工作台返回的引擎目录格式无效。', null);
      if (mounted) { setHealth('ready'); setCapabilities(cap); setGpuInventory(gpuInfo); setEngineCatalog(engineData.engines); setSpecializedCatalog(specialized); if (specialized.models.length) setNewSpecializedModel(specialized.models[0].id); }
    })
      .catch(() => { if (mounted) setHealth('offline'); });
    void refreshLists();
    void api<{ materials: MaterialCatalogItem[] }>('/api/materials').then(result => {
      if (!Array.isArray(result.materials)) throw new ApiError('本机工作台返回的材料目录格式无效。', null);
      setMaterials(result.materials);
    }).catch(error => setNotice(`材料目录加载失败：${prettyError(error)}`));
    return () => { mounted = false; };
  }, [refreshLists]);

  return {
    projectRows, setProjectRows,
    runs, setRuns,
    capabilities, setCapabilities,
    gpuInventory, setGpuInventory,
    engineCatalog, setEngineCatalog,
    materials, setMaterials,
    specializedCatalog, setSpecializedCatalog,
    newSpecializedModel, setNewSpecializedModel,
    health, setHealth,
    notice, setNotice,
    refreshLists,
  };
}
