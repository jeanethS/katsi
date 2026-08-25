import { useEffect, useState, type FormEvent } from "react";
import { BookOpen, ChatCircleText, Gear } from "@phosphor-icons/react";
import { Button, Card, SourceChip } from "./components/primitives";
import { request } from "./api/client";
import type { IndexResult, Status as ApiStatus } from "./api/types";
import { useT } from "./i18n/useT";
import { useUiStore } from "./stores/ui";
import { GalaxyPanel } from "./webgl/GalaxyPanel";
import { IngestPulse } from "./webgl/IngestPulse";
import { KnowledgeGalaxy } from "./webgl/KnowledgeGalaxy";
import { useGraphData } from "./webgl/useGraphData";
import type { GraphNode } from "./api/types";

type Route = "library" | "ask" | "status" | "dev";
type Mode = "auto" | "local" | "cloud";
type FolderStatus = "ready" | "indexing" | "indexed" | "error";

interface LibraryFolder {
  id: string;
  name: string;
  path: string;
  status: FolderStatus;
  detail?: string;
  progress?: number;
}

const modeKeys: Record<Mode, "ask.auto" | "ask.local" | "ask.cloud"> = {
  auto: "ask.auto",
  local: "ask.local",
  cloud: "ask.cloud",
};

const routes: Record<Exclude<Route, "dev">, { icon: typeof BookOpen }> = {
  library: { icon: BookOpen },
  ask: { icon: ChatCircleText },
  status: { icon: Gear },
};

function currentRoute(): Route {
  const route = window.location.hash.replace("#", "");
  return route === "library" || route === "ask" || route === "status" || route === "dev" ? route : "library";
}

function Receipt({ mode, t }: { mode: Mode; t: ReturnType<typeof useT> }) {
  const cloud = mode === "cloud";
  const rows = [
    [t("receipt.mode"), cloud ? t("ask.cloud") : mode === "local" ? t("ask.local") : t("ask.auto")],
    [t("receipt.files"), "4"],
    [t("receipt.chunks"), "6"],
    [t("receipt.localTokens"), "1,842"],
    [t("receipt.cloudTokens"), cloud ? "936" : "0"],
    [t("receipt.time"), "3.2s"],
  ];
  return (
    <details className="receipt" open>
      <summary>{t("receipt.title")}</summary>
      <dl>
        {rows.map(([label, value]) => (
          <div className="receipt-row" key={label}>
            <dt>{label}</dt>
            <span aria-hidden="true" className="leaders" />
            <dd className={label === t("receipt.cloudTokens") && cloud ? "semantic-cloud" : ""}>
              {value}
              {label === t("receipt.mode") && <span className={cloud ? "semantic-cloud" : "semantic-local"}> ●</span>}
            </dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

function Library({ t }: { t: ReturnType<typeof useT> }) {
  const [hasFolder, setHasFolder] = useState(true);
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [addingFolder, setAddingFolder] = useState(false);
  const [folderPath, setFolderPath] = useState("");
  const [folders, setFolders] = useState<LibraryFolder[]>(() => [
    { id: "notes", name: t("folder.name"), path: t("folder.path"), status: "indexed", detail: t("folder.meta") },
    { id: "research", name: t("folder.research"), path: t("folder.researchPath"), status: "ready" },
    { id: "projects", name: t("folder.projects"), path: t("folder.projectsPath"), status: "indexing", detail: "118 / 412 / china-trip.md", progress: 29 },
  ]);
  const { data, error, loading } = useGraphData();
  const addFolder = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const path = folderPath.trim();
    if (!path) return;
    const name = path.split(/[\\/]/).filter(Boolean).at(-1) ?? path;
    setFolders((current) => [...current, { id: crypto.randomUUID(), name, path, status: "ready" }]);
    setFolderPath("");
    setAddingFolder(false);
    setHasFolder(true);
  };
  const indexFolder = async (id: string) => {
    const folder = folders.find((entry) => entry.id === id);
    if (!folder || folder.status === "indexing") return;
    setFolders((current) => current.map((entry) => entry.id === id ? { ...entry, status: "indexing", detail: t("library.indexing"), progress: 10 } : entry));
    try {
      const result = await request<IndexResult>("/api/index", { method: "POST", body: JSON.stringify({ path: folder.path }) });
      setFolders((current) => current.map((entry) => entry.id === id ? { ...entry, status: result.error ? "error" : "indexed", detail: `${result.indexed} / ${result.total} ${t("library.indexed")}`, progress: 100 } : entry));
    } catch (cause: unknown) {
      setFolders((current) => current.map((entry) => entry.id === id ? { ...entry, status: "error", detail: (cause as Error).message, progress: undefined } : entry));
    }
  };
  return (
    <section className="screen library-screen">
      <header className="screen-header">
        <h1 className="display screen-title">{t("library.title")}</h1>
        <Button onClick={() => setAddingFolder(true)}>{t("library.addFolder")}</Button>
      </header>
      {addingFolder && <form className="folder-form" onSubmit={addFolder}><label htmlFor="folder-path">{t("library.folderPath")}</label><div><input autoFocus id="folder-path" onChange={(event) => setFolderPath(event.target.value)} placeholder={t("library.folderPlaceholder")} value={folderPath} /><Button type="submit">{t("library.add")}</Button><Button onClick={() => setAddingFolder(false)} type="button" variant="ghost">{t("folder.cancel")}</Button></div></form>}
      {hasFolder ? (
        <div className="library-workspace">
          <div className="folder-list">
            {folders.map((folder) => <Card key={folder.id}>
              <div className="folder-row">
                <div><p className="folder-name">{folder.name}</p><p className="path">{folder.path}</p></div>
                {folder.status === "indexing" ? <Button disabled variant="ghost">{t("library.indexing")}</Button> : <Button onClick={() => indexFolder(folder.id)}>{t("library.index")}</Button>}
              </div>
              {folder.detail && <p className={`metadata ${folder.status === "error" ? "folder-error" : ""}`}><span className={folder.status === "error" ? "status-dot is-error" : "status-dot"} />{folder.detail}</p>}
              {folder.status === "indexing" && <IngestPulse label={t("library.ingesting")} value={folder.progress ?? 10} />}
            </Card>)}
          </div>
          <section className="galaxy-area" aria-label={t("library.graphTitle")}>
            <div className="galaxy-header"><div><p className="eyebrow">{t("library.graphEyebrow")}</p><h2 className="display">{t("library.graphTitle")}</h2></div><p>{loading ? t("library.graphLoading") : error ? t("library.graphOffline") : t("library.graphReady")}</p></div>
            <KnowledgeGalaxy data={data} loading={loading} onSelect={setSelectedNode} selectedId={selectedNode?.id} />
            <GalaxyPanel data={data} emptyLabel={t("library.graphSelect")} node={selectedNode} relationshipLabel={t("library.relationships")} />
          </section>
        </div>
      ) : (
        <div className="empty-state">
          <h2 className="display">{t("library.emptyTitle")}</h2>
          <p>{t("library.emptyBody")}</p>
          <Button onClick={() => setAddingFolder(true)}>{t("library.addFolder")}</Button>
        </div>
      )}
    </section>
  );
}

function Ask({ t }: { t: ReturnType<typeof useT> }) {
  const [mode, setMode] = useState<Mode>("local");
  const [question, setQuestion] = useState("");
  const [asked, setAsked] = useState(true);
  const submit = () => { if (question.trim()) setAsked(true); };
  return (
    <section className="ask-screen">
      <h1 className="display screen-title">{t("ask.title")}</h1>
      {asked && <>
        <p className="question">{question || t("ask.question")}</p>
        <div className="sources" aria-label={t("ask.sources")}>
          <SourceChip name={t("source.one")} summary={t("source.summary")} why={t("source.why")} />
          <SourceChip name={t("source.two")} summary={t("source.summary")} why={t("source.why")} />
          <button className="source-chip" type="button">{t("ask.graph")}</button>
        </div>
        <p aria-live="polite" className="answer">{t("ask.answer")}</p>
        <Receipt mode={mode} t={t} />
        <div className="utility-row">
          <Button variant="ghost">{t("ask.copy")}</Button>
          <Button variant="ghost" onClick={() => setMode("local")}>{t("ask.reaskLocal")}</Button>
        </div>
      </>}
      <form className="composer" onSubmit={(event) => { event.preventDefault(); submit(); }}>
        <textarea aria-label={t("ask.placeholder")} onChange={(event) => setQuestion(event.target.value)} placeholder={t("ask.placeholder")} value={question} />
        <div className="segmented" aria-label="Synthesis mode">
          {(["auto", "local", "cloud"] as Mode[]).map((entry) => (
            <button
              className={`segment ${mode === entry ? `is-${entry}` : ""}`}
              disabled={entry === "cloud"}
              key={entry}
              onClick={() => setMode(entry)}
              title={entry === "cloud" ? t("ask.cloudUnavailable") : undefined}
              type="button"
            >
              {t(modeKeys[entry])}
            </button>
          ))}
        </div>
        <Button>{t("ask.submit")}</Button>
      </form>
    </section>
  );
}

function Status({ t }: { t: ReturnType<typeof useT> }) {
  const [status, setStatus] = useState<ApiStatus | null>(null);
  const [error, setError] = useState(false);
  const [reload, setReload] = useState(0);
  const { language, setLanguage, theme, setTheme } = useUiStore();
  useEffect(() => {
    const controller = new AbortController();
    setError(false);
    request<ApiStatus>("/api/status", { signal: controller.signal })
      .then(setStatus)
      .catch((cause: unknown) => {
        if ((cause as Error).name !== "AbortError") setError(true);
      });
    return () => controller.abort();
  }, [reload]);
  return (
    <section className="screen stack">
      <h1 className="display screen-title">{t("status.title")}</h1>
      {!status && !error && <Card><p>{t("status.loading")}</p></Card>}
      {error && <Card><div role="alert"><h2 className="display section-heading">{t("status.apiError")}</h2><Button onClick={() => setReload((value) => value + 1)}>{t("status.retry")}</Button></div></Card>}
      {status && <>
        <Card><h2 className="display section-heading">{t("status.engine")}</h2><div className="status-grid"><p><span className={`status-dot ${status.ollama.reachable ? "" : "is-error"}`} />{status.ollama.reachable ? t("status.reachable") : t("error.title")}</p><p className="engine-data">{status.ollama.models.join(" / ") || t("status.noModels")}</p><p className="engine-data">{status.counts.files} {t("status.files")} / {status.counts.chunks} {t("status.chunks")} / {status.counts.entities} {t("status.entities")} / {status.counts.topics} {t("status.topics")}</p><p className="engine-data">{status.db_bytes.vectors} B {t("status.vectors")} / {status.db_bytes.graph} B {t("status.graph")}</p></div></Card>
        <Card><h2 className="display section-heading">{t("status.synthesis")}</h2><p className="engine-data">{status.synth.backend} / {status.synth.cloud_configured ? t("privacy.cloud") : t("status.cloudUnavailable")}</p></Card>
      </>}
      <Card>
        <h2 className="display section-heading">{t("status.about")}</h2>
        <div className="settings-row"><label>{t("status.language")}</label><div className="segmented"><button className={`segment ${language === "en" ? "is-auto" : ""}`} onClick={() => setLanguage("en")} type="button">EN</button><button className={`segment ${language === "es" ? "is-auto" : ""}`} onClick={() => setLanguage("es")} type="button">ES</button></div></div>
        <div className="settings-row"><label>{t("status.theme")}</label><div className="segmented"><button className={`segment ${theme === "dark" ? "is-auto" : ""}`} onClick={() => setTheme("dark")} type="button">{t("status.dark")}</button><button className={`segment ${theme === "light" ? "is-auto" : ""}`} onClick={() => setTheme("light")} type="button">{t("status.light")}</button></div></div>
      </Card>
    </section>
  );
}

function Dev({ t }: { t: ReturnType<typeof useT> }) {
  return <section className="screen"><header className="screen-header"><div><h1 className="display screen-title">{t("demo.title")}</h1><p>{t("demo.description")}</p></div></header><div className="dev-grid"><Card><Button>{t("library.addFolder")}</Button> <Button variant="ghost">{t("ask.copy")}</Button> <Button variant="danger">Remove</Button></Card><Card><Receipt mode="local" t={t} /></Card></div></section>;
}

export default function App() {
  const [route, setRoute] = useState<Route>(currentRoute);
  const { theme } = useUiStore();
  const t = useT();
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);
  useEffect(() => {
    const onHashChange = () => setRoute(currentRoute());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);
  const navigate = (next: Route) => { window.location.hash = next; };
  return <div className="app-shell"><aside className="rail"><button className="wordmark" onClick={() => navigate("library")} type="button">katsi</button><nav className="nav">{(Object.keys(routes) as Exclude<Route, "dev">[]).map((entry) => { const Icon = routes[entry].icon; return <button className={`nav-link ${route === entry ? "is-active" : ""}`} key={entry} onClick={() => navigate(entry)} type="button"><Icon size={18} weight="regular" /><span>{t(`nav.${entry}`)}</span></button>; })}</nav><div className="rail-spacer" /><p className="privacy"><span className="privacy-local">●</span> {t("privacy.local")}</p></aside><main className="main">{route === "library" && <Library t={t} />}{route === "ask" && <Ask t={t} />}{route === "status" && <Status t={t} />}{route === "dev" && <Dev t={t} />}</main></div>;
}
