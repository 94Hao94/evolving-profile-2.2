"use client";

import { useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { toast } from "sonner";
import { BankSelector } from "@/components/bank-selector";
import { Sidebar } from "@/components/sidebar";
import { DataView } from "@/components/data-view";
import { normalizeDataSubTab, type DataSubTab } from "@/lib/data-subtab";
import { DocumentsView } from "@/components/documents-view";
import { EntitiesView } from "@/components/entities-view";
import { AgentMemoryView } from "@/components/agent-memory-view";
import { SearchDebugView } from "@/components/search-debug-view";
import { BankProfileView } from "@/components/bank-profile-view";
import { RuntimeSectionsView } from "@/components/runtime-sections-view";
import { OperationalOverview } from "@/components/operational-overview";
import { MemoryDefenseSection } from "@/components/memory-defense-section";
import { BankStatsView } from "@/components/bank-stats-view";
import { BankOperationsView } from "@/components/bank-operations-view";
import { MentalModelsView } from "@/components/mental-models-view";
import { PreferenceView } from "@/components/preference-view";
import { ContextMemoryView } from "@/components/context-memory-view";
import { FlowView } from "@/components/flow-view";
import { AuditLogsView } from "@/components/audit-logs-view";
import { LLMRequestsView } from "@/components/llm-requests-view";
import { MemoryQualityEngineView } from "@/components/memory-quality-engine-view";
import { FeatureNotEnabled } from "@/components/feature-not-enabled";
import { useFeatures } from "@/lib/features-context";
import { useBank } from "@/lib/bank-context";
import { bankRoute } from "@/lib/bank-url";
import { client } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { ActionButton, ActionMenuItem } from "@/components/ui/action-button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Brain,
  Download,
  Trash2,
  Loader2,
  MoreVertical,
  Pencil,
  RotateCcw,
  Activity,
  FlaskConical,
} from "lucide-react";
import { LlmHealthDialog } from "@/components/llm-health-dialog";
import { ExtractDialog } from "@/components/extract-dialog";

import { inlineUiText, setInlineLocale } from "@/lib/inline-i18n";
type NavItem = "recall" | "data" | "documents" | "agent-memory" | "flow" | "quality" | "profile";
type BankConfigTab =
  | "general"
  | "data"
  | "memory-defense"
  | "configuration"
  | "memory"
  | "models"
  | "rag"
  | "providers"
  | "scenario"
  | "backup"
  | "audit-logs"
  | "llm-requests";

export default function BankPage() {
  const params = useParams();
  const router = useRouter();
  const searchParams = useSearchParams();
  const t = useTranslations("bank");
  const locale = useLocale();
  setInlineLocale(locale);
  const tCommon = useTranslations("common");
  const tAction = useTranslations("actionFeedback");
  const { features } = useFeatures();
  const { currentBank: bankId, setCurrentBank, loadBanks } = useBank();

  const rawView = searchParams.get("view") || "profile";
  // The former Reflect screen issued foreground model calls. Keep old bookmarks
  // usable while routing them to the read-only observable chain instead.
  const view = (rawView === "reflect" ? "flow" : rawView) as NavItem;
  const rawSubTab = searchParams.get("subTab");
  const subTab: DataSubTab = normalizeDataSubTab(rawSubTab);
  const bankConfigTab = (searchParams.get("bankConfigTab") || "general") as BankConfigTab;
  const observationsEnabled = features?.observations ?? false;
  const bankConfigEnabled = features?.bank_config_api ?? false;
  const auditLogEnabled = features?.audit_log ?? false;
  const llmTraceEnabled = features?.llm_trace ?? false;
  const llmHealthEnabled = features?.bank_llm_health ?? false;

  // Bank actions state
  const [showLlmHealthDialog, setShowLlmHealthDialog] = useState(false);
  const [showExtractDialog, setShowExtractDialog] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [showClearObservationsDialog, setShowClearObservationsDialog] = useState(false);
  const [isClearingObservations, setIsClearingObservations] = useState(false);
  const [isConsolidating, setIsConsolidating] = useState(false);
  const [isRecoveringConsolidation, setIsRecoveringConsolidation] = useState(false);
  const [showResetConfigDialog, setShowResetConfigDialog] = useState(false);
  const [isResettingConfig, setIsResettingConfig] = useState(false);

  const handleTabChange = (tab: NavItem) => {
    if (!bankId) return;
    router.push(bankRoute(bankId, `?view=${tab}`));
  };

  const handleDataSubTabChange = (newSubTab: DataSubTab) => {
    if (!bankId) return;
    router.push(bankRoute(bankId, `?view=data&subTab=${newSubTab}`));
  };

  const handleBankConfigTabChange = (newTab: BankConfigTab) => {
    if (!bankId) return;
    router.push(bankRoute(bankId, `?view=profile&bankConfigTab=${newTab}`));
  };

  const handleDeleteBank = async () => {
    if (!bankId) return false;

    setIsDeleting(true);
    try {
      await client.deleteBank(bankId);
      setShowDeleteDialog(false);
      setCurrentBank(null);
      await loadBanks();
      router.push("/");
    } catch (error) {
      throw error;
    } finally {
      setIsDeleting(false);
    }
  };

  const handleClearObservations = async () => {
    if (!bankId) return false;

    setIsClearingObservations(true);
    try {
      const result = await client.clearObservations(bankId);
      toast.success(t("observationsCleared"), {
        description: result.message || t("observationsClearedDefault"),
      });
    } catch (error) {
      throw error;
    } finally {
      setIsClearingObservations(false);
    }
  };

  const handleResetConfig = async () => {
    if (!bankId) return false;
    setIsResettingConfig(true);
    try {
      await client.resetBankConfig(bankId);
    } catch (error) {
      throw error;
    } finally {
      setIsResettingConfig(false);
    }
  };

  const handleTriggerConsolidation = async () => {
    if (!bankId) return false;

    setIsConsolidating(true);
    try {
      await client.triggerConsolidation(bankId);
    } catch (error) {
      throw error;
    } finally {
      setIsConsolidating(false);
    }
  };

  const handleRecoverConsolidation = async () => {
    if (!bankId) return false;

    setIsRecoveringConsolidation(true);
    try {
      const result = await client.recoverConsolidation(bankId);
      toast.success(t("recoveredMemories", { count: result.retried_count }));
    } catch (error) {
      throw error;
    } finally {
      setIsRecoveringConsolidation(false);
    }
  };

  return (
    <div className="min-h-screen bg-background flex flex-col">
      <BankSelector />

      <div className="flex min-w-0 flex-1 overflow-hidden">
        <Sidebar currentTab={view} onTabChange={handleTabChange} />

        <main className="min-w-0 flex-1 overflow-x-hidden overflow-y-auto">
          <div className="p-3 sm:p-6">
            {/* Bank Configuration Tab */}
            {view === "profile" && (
              <div>
                <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h1 className="text-3xl font-bold mb-2 text-foreground">
                      {t("bankConfiguration")}
                    </h1>
                    <p className="text-muted-foreground">{t("bankConfigurationDescription")}</p>
                  </div>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button variant="outline" size="sm">
                        {t("actions")}
                        <MoreVertical className="w-4 h-4 ml-2" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" className="w-48">
                      <ActionMenuItem
                        onAction={async () => {
                          if (!bankId) return false;
                          try {
                            const manifest = await client.exportBankTemplate(bankId);
                            const json = JSON.stringify(manifest, null, 2);
                            await navigator.clipboard.writeText(json);
                            toast.success(t("templateCopied"));
                          } catch (error) {
                            throw error;
                          }
                        }}
                      >
                        <Download className="w-4 h-4 mr-2" />
                        {t("exportTemplate")}
                      </ActionMenuItem>
                      <DropdownMenuItem onClick={() => setShowExtractDialog(true)}>
                        <FlaskConical className="w-4 h-4 mr-2" />
                        {t("dryRunExtraction")}
                      </DropdownMenuItem>
                      {llmHealthEnabled && (
                        <DropdownMenuItem onClick={() => setShowLlmHealthDialog(true)}>
                          <Activity className="w-4 h-4 mr-2" />
                          {t("health")}
                        </DropdownMenuItem>
                      )}
                      <DropdownMenuSeparator />
                      <ActionMenuItem
                        onAction={handleTriggerConsolidation}
                        successLabel={tAction("submitted")}
                        disabled={isConsolidating || !observationsEnabled}
                        title={
                          !observationsEnabled ? "Observations feature is not enabled" : undefined
                        }
                      >
                        {isConsolidating ? (
                          <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                        ) : (
                          <Brain className="w-4 h-4 mr-2" />
                        )}
                        {isConsolidating ? t("consolidating") : t("runConsolidation")}
                        {!observationsEnabled && (
                          <span className="ml-auto text-xs text-muted-foreground">Off</span>
                        )}
                      </ActionMenuItem>
                      <ActionMenuItem
                        onAction={handleRecoverConsolidation}
                        successLabel={tAction("submitted")}
                        disabled={isRecoveringConsolidation || !observationsEnabled}
                        title={
                          !observationsEnabled ? "Observations feature is not enabled" : undefined
                        }
                      >
                        {isRecoveringConsolidation ? (
                          <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                        ) : (
                          <RotateCcw className="w-4 h-4 mr-2" />
                        )}
                        {isRecoveringConsolidation ? t("recovering") : t("recoverConsolidation")}
                        {!observationsEnabled && (
                          <span className="ml-auto text-xs text-muted-foreground">Off</span>
                        )}
                      </ActionMenuItem>
                      <DropdownMenuItem
                        onClick={() => setShowClearObservationsDialog(true)}
                        disabled={!observationsEnabled}
                        className="text-amber-600 dark:text-amber-400 focus:text-amber-700 dark:focus:text-amber-300"
                        title={
                          !observationsEnabled ? "Observations feature is not enabled" : undefined
                        }
                      >
                        <Trash2 className="w-4 h-4 mr-2" />
                        {t("clearObservations")}
                        {!observationsEnabled && (
                          <span className="ml-auto text-xs text-muted-foreground">Off</span>
                        )}
                      </DropdownMenuItem>
                      <DropdownMenuSeparator />
                      <DropdownMenuItem
                        onClick={() => setShowResetConfigDialog(true)}
                        disabled={!bankConfigEnabled}
                        className="text-amber-600 dark:text-amber-400 focus:text-amber-700 dark:focus:text-amber-300"
                        title={!bankConfigEnabled ? "Bank Config API is disabled" : undefined}
                      >
                        <RotateCcw className="w-4 h-4 mr-2" />
                        {t("resetConfiguration")}
                        {!bankConfigEnabled && (
                          <span className="ml-auto text-xs text-muted-foreground">Off</span>
                        )}
                      </DropdownMenuItem>
                      <DropdownMenuSeparator />
                      <DropdownMenuItem
                        onClick={() => setShowDeleteDialog(true)}
                        className="text-red-600 dark:text-red-400 focus:text-red-700 dark:focus:text-red-300"
                      >
                        <Trash2 className="w-4 h-4 mr-2" />
                        {t("deleteBank")}
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>

                {/* Sub-tabs */}
                <div className="mb-6 overflow-x-auto border-b border-border [scrollbar-width:thin]">
                  <div className="flex min-w-max gap-1">
                    <button
                      onClick={() => handleBankConfigTabChange("general")}
                      className={`relative whitespace-nowrap px-3 py-3 text-sm font-semibold transition-colors sm:px-6 ${
                        bankConfigTab === "general"
                          ? "text-primary"
                          : "text-muted-foreground hover:text-foreground"
                      }`}
                    >
                      {t("general")}
                      {bankConfigTab === "general" && (
                        <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-primary" />
                      )}
                    </button>
                    {bankConfigEnabled && (
                      <button
                        onClick={() => handleBankConfigTabChange("memory-defense")}
                        className={`relative whitespace-nowrap px-3 py-3 text-sm font-semibold transition-colors sm:px-6 ${
                          bankConfigTab === "memory-defense"
                            ? "text-primary"
                            : "text-muted-foreground hover:text-foreground"
                        }`}
                      >
                        {locale.startsWith("zh")
                          ? inlineUiText("数据与路由防护")
                          : "Data and routing protection"}
                        {bankConfigTab === "memory-defense" && (
                          <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-primary" />
                        )}
                      </button>
                    )}
                    {
                      <button
                        onClick={() => handleBankConfigTabChange("configuration")}
                        className={`relative whitespace-nowrap px-3 py-3 text-sm font-semibold transition-colors sm:px-6 ${
                          bankConfigTab === "configuration"
                            ? "text-primary"
                            : "text-muted-foreground hover:text-foreground"
                        }`}
                      >
                        {locale.startsWith("zh")
                          ? inlineUiText("运行配置")
                          : "Runtime configuration"}
                        {bankConfigTab === "configuration" && (
                          <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-primary" />
                        )}
                      </button>
                    }
                    <button
                      onClick={() => handleBankConfigTabChange("memory")}
                      className={`relative whitespace-nowrap px-3 py-3 text-sm font-semibold ${bankConfigTab === "memory" ? "text-primary" : "text-muted-foreground hover:text-foreground"}`}
                    >
                      {locale.startsWith("zh") ? inlineUiText("EP 记忆") : "EP Memory"}
                    </button>
                    <button
                      onClick={() => handleBankConfigTabChange("models")}
                      className={`relative whitespace-nowrap px-3 py-3 text-sm font-semibold ${bankConfigTab === "models" ? "text-primary" : "text-muted-foreground hover:text-foreground"}`}
                    >
                      {locale.startsWith("zh")
                        ? inlineUiText("检索与判断模型")
                        : "Retrieval and judge models"}
                    </button>
                    <button
                      onClick={() => handleBankConfigTabChange("rag")}
                      className={`relative whitespace-nowrap px-3 py-3 text-sm font-semibold ${bankConfigTab === "rag" ? "text-primary" : "text-muted-foreground hover:text-foreground"}`}
                    >
                      {locale.startsWith("zh") ? inlineUiText("外部 RAG") : "External RAG"}
                    </button>
                    <button
                      onClick={() => handleBankConfigTabChange("providers")}
                      className={`relative whitespace-nowrap px-3 py-3 text-sm font-semibold ${bankConfigTab === "providers" ? "text-primary" : "text-muted-foreground hover:text-foreground"}`}
                    >
                      {locale.startsWith("zh")
                        ? inlineUiText("Provider 与 Fallback")
                        : "Providers and fallback"}
                    </button>
                    <button
                      onClick={() => handleBankConfigTabChange("scenario")}
                      className={`relative whitespace-nowrap px-3 py-3 text-sm font-semibold ${bankConfigTab === "scenario" ? "text-primary" : "text-muted-foreground hover:text-foreground"}`}
                    >
                      {locale.startsWith("zh") ? inlineUiText("情景摘要") : "Scenario summaries"}
                    </button>
                    <button
                      onClick={() => handleBankConfigTabChange("backup")}
                      className={`relative whitespace-nowrap px-3 py-3 text-sm font-semibold ${bankConfigTab === "backup" ? "text-primary" : "text-muted-foreground hover:text-foreground"}`}
                    >
                      {locale.startsWith("zh") ? inlineUiText("备份") : "Backup"}
                    </button>
                    <button
                      onClick={() => handleBankConfigTabChange("audit-logs")}
                      className={`relative whitespace-nowrap px-3 py-3 text-sm font-semibold transition-colors sm:px-6 ${
                        bankConfigTab === "audit-logs"
                          ? "text-primary"
                          : "text-muted-foreground hover:text-foreground"
                      }`}
                    >
                      {locale.startsWith("zh") ? inlineUiText("审计日志") : t("auditLogs")}
                      {!auditLogEnabled && (
                        <span className="ml-2 text-xs px-1.5 py-0.5 rounded bg-muted text-muted-foreground">
                          {locale.startsWith("zh") ? inlineUiText("关闭") : "Off"}
                        </span>
                      )}
                      {bankConfigTab === "audit-logs" && (
                        <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-primary" />
                      )}
                    </button>
                    <button
                      onClick={() => handleBankConfigTabChange("llm-requests")}
                      className={`relative whitespace-nowrap px-3 py-3 text-sm font-semibold transition-colors sm:px-6 ${
                        bankConfigTab === "llm-requests"
                          ? "text-primary"
                          : "text-muted-foreground hover:text-foreground"
                      }`}
                    >
                      {locale.startsWith("zh") ? inlineUiText("模型请求") : t("llmRequests")}
                      {!llmTraceEnabled && (
                        <span className="ml-2 text-xs px-1.5 py-0.5 rounded bg-muted text-muted-foreground">
                          {locale.startsWith("zh") ? inlineUiText("关闭") : "Off"}
                        </span>
                      )}
                      {bankConfigTab === "llm-requests" && (
                        <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-primary" />
                      )}
                    </button>
                  </div>
                </div>

                {/* Tab content */}
                <div>
                  {bankConfigTab === "general" && (
                    <div>
                      <OperationalOverview />
                      <div className="space-y-6">
                        <BankStatsView />
                        <div id="bank-operations">
                          <BankOperationsView />
                        </div>
                        <BankProfileView hideReflectFields />
                      </div>
                    </div>
                  )}
                  {bankConfigTab === "memory-defense" && bankConfigEnabled && bankId && (
                    <div className="space-y-6">
                      <MemoryDefenseSection bankId={bankId} />
                    </div>
                  )}
                  {bankConfigTab === "configuration" && (
                    <div className="space-y-6">
                      <RuntimeSectionsView section="runtime" />
                    </div>
                  )}
                  {bankConfigTab === "memory" && <RuntimeSectionsView section="memory" />}
                  {bankConfigTab === "models" && <RuntimeSectionsView section="models" />}
                  {bankConfigTab === "rag" && <RuntimeSectionsView section="rag" />}
                  {bankConfigTab === "providers" && <RuntimeSectionsView section="providers" />}
                  {bankConfigTab === "scenario" && <RuntimeSectionsView section="scenario" />}
                  {bankConfigTab === "backup" && <RuntimeSectionsView section="backup" />}
                  {bankConfigTab === "audit-logs" &&
                    (auditLogEnabled ? (
                      <div>
                        <p className="text-sm text-muted-foreground mb-4">
                          {locale.startsWith("zh")
                            ? inlineUiText(
                                "原始操作记录，包含当前 Evolving Profile 与迁移前底层操作；当前宿主链路以“运行配置”和“链路”页为准。"
                              )
                            : "Raw operation records include current Evolving Profile activity and pre-migration records. Use Runtime Configuration and Flow for the active host path."}
                        </p>
                        <AuditLogsView />
                      </div>
                    ) : (
                      <FeatureNotEnabled
                        title={t("auditLogsNotEnabled")}
                        description={t.rich("auditLogsDisabledMessage", {
                          envVar: () => (
                            <code className="px-1 py-0.5 bg-muted rounded text-xs">
                              HINDSIGHT_API_AUDIT_LOG_ENABLED=true
                            </code>
                          ),
                        })}
                      />
                    ))}
                  {bankConfigTab === "llm-requests" &&
                    (llmTraceEnabled ? (
                      <div>
                        <p className="text-sm text-muted-foreground mb-4">
                          {inlineUiText(
                            "原始模型调用记录，包含迁移前后台任务；前台查询不会因浏览该页额外调用模型。"
                          )}
                        </p>
                        <LLMRequestsView />
                      </div>
                    ) : (
                      <FeatureNotEnabled
                        title={t("llmRequestsNotEnabled")}
                        description={t.rich("llmRequestsDisabledMessage", {
                          envVar: () => (
                            <code className="px-1 py-0.5 bg-muted rounded text-xs">
                              HINDSIGHT_API_LLM_TRACE_ENABLED=true
                            </code>
                          ),
                        })}
                      />
                    ))}
                </div>
              </div>
            )}

            {/* Recall Tab */}
            {view === "recall" && (
              <div>
                <h1 className="text-3xl font-bold mb-2 text-foreground">{t("recallAnalyzer")}</h1>
                <p className="text-muted-foreground mb-6">{t("recallAnalyzerDescription")}</p>
                <SearchDebugView />
              </div>
            )}

            {/* Data/Memories Tab */}
            {view === "data" && (
              <div>
                <h1 className="text-3xl font-bold mb-2 text-foreground">{t("memories")}</h1>
                <p className="text-muted-foreground mb-6">{t("memoriesDescription")}</p>

                <div className="mb-6 border-b border-border">
                  <div className="flex flex-wrap gap-1">
                    <button
                      onClick={() => handleDataSubTabChange("world")}
                      className={`relative whitespace-nowrap px-3 py-3 text-sm font-semibold transition-colors sm:px-6 ${
                        subTab === "world"
                          ? "text-primary"
                          : "text-muted-foreground hover:text-foreground"
                      }`}
                    >
                      {t("worldFacts")}
                      {subTab === "world" && (
                        <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-primary" />
                      )}
                    </button>
                    <button
                      onClick={() => handleDataSubTabChange("experience")}
                      className={`relative whitespace-nowrap px-3 py-3 text-sm font-semibold transition-colors sm:px-6 ${
                        subTab === "experience"
                          ? "text-primary"
                          : "text-muted-foreground hover:text-foreground"
                      }`}
                    >
                      {t("experience")}
                      {subTab === "experience" && (
                        <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-primary" />
                      )}
                    </button>
                    <button
                      onClick={() => handleDataSubTabChange("entities")}
                      className={`relative whitespace-nowrap px-3 py-3 text-sm font-semibold transition-colors sm:px-6 ${
                        subTab === "entities"
                          ? "text-primary"
                          : "text-muted-foreground hover:text-foreground"
                      }`}
                    >
                      {t("entities")}
                      {subTab === "entities" && (
                        <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-primary" />
                      )}
                    </button>
                    <button
                      onClick={() => handleDataSubTabChange("preferences")}
                      className={`relative whitespace-nowrap px-3 py-3 text-sm font-semibold transition-colors sm:px-6 ${
                        subTab === "preferences"
                          ? "text-primary"
                          : "text-muted-foreground hover:text-foreground"
                      }`}
                    >
                      {locale.startsWith("zh")
                        ? inlineUiText("多维度偏好")
                        : "Multi-dimensional Preferences"}
                      {subTab === "preferences" && (
                        <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-primary" />
                      )}
                    </button>
                    <button
                      onClick={() => handleDataSubTabChange("context")}
                      className={`relative whitespace-nowrap px-3 py-3 text-sm font-semibold transition-colors sm:px-6 ${
                        subTab === "context"
                          ? "text-primary"
                          : "text-muted-foreground hover:text-foreground"
                      }`}
                    >
                      {locale.startsWith("zh") ? inlineUiText("情景摘要") : "Scenario Summary"}
                      {subTab === "context" && (
                        <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-primary" />
                      )}
                    </button>
                  </div>
                </div>

                <div>
                  {subTab === "world" && (
                    <div>
                      <p className="text-sm text-muted-foreground mb-4">
                        {t("worldFactsDescription")}
                      </p>
                      <DataView key="world" factType="world" />
                    </div>
                  )}
                  {subTab === "experience" && (
                    <div>
                      <p className="text-sm text-muted-foreground mb-4">
                        {t("experienceDescription")}
                      </p>
                      <DataView key="experience" factType="experience" />
                    </div>
                  )}
                  {subTab === "entities" && <EntitiesView />}
                  {subTab === "preferences" && <PreferenceView />}
                  {subTab === "context" && <ContextMemoryView bankId={bankId} />}
                </div>
              </div>
            )}

            {/* Documents Tab — DocumentsView renders its own title row so the
                Export/Import Actions menu can sit beside the heading. */}
            {view === "documents" && (
              <div>
                <DocumentsView />
              </div>
            )}

            {/* Agent Memory Tab */}
            {view === "agent-memory" && (
              <div>
                <AgentMemoryView />
              </div>
            )}

            {view === "flow" && <FlowView />}
            {view === "quality" && <MemoryQualityEngineView />}
          </div>
        </main>
      </div>

      {/* LLM connectivity check */}
      {bankId && (
        <LlmHealthDialog
          bankId={bankId}
          open={showLlmHealthDialog}
          onOpenChange={setShowLlmHealthDialog}
        />
      )}

      {/* Dry-run extraction */}
      <ExtractDialog open={showExtractDialog} onOpenChange={setShowExtractDialog} />

      {/* Delete Bank Confirmation Dialog */}
      <AlertDialog open={showDeleteDialog} onOpenChange={setShowDeleteDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("deleteMemoryBank")}</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2 text-sm text-muted-foreground">
                <p>
                  {t.rich("deleteBankPrompt", {
                    bankName: () => <span className="font-semibold text-foreground">{bankId}</span>,
                  })}
                </p>
                <p className="text-red-600 dark:text-red-400 font-medium">
                  {t("deleteBankWarning")}
                </p>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isDeleting}>{tCommon("cancel")}</AlertDialogCancel>
            <ActionButton
              onAction={handleDeleteBank}
              disabled={isDeleting}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {isDeleting ? (
                <>
                  <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                  {t("deleting")}
                </>
              ) : (
                <>
                  <Trash2 className="w-4 h-4 mr-2" />
                  {t("deleteBank")}
                </>
              )}
            </ActionButton>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Reset Configuration Confirmation Dialog */}
      <AlertDialog open={showResetConfigDialog} onOpenChange={setShowResetConfigDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("resetConfigTitle")}</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2 text-sm text-muted-foreground">
                <p>
                  {t.rich("resetConfigPrompt", {
                    bankName: () => <span className="font-semibold text-foreground">{bankId}</span>,
                  })}
                </p>
                <p className="text-amber-600 dark:text-amber-400 font-medium">
                  {t("resetConfigWarning")}
                </p>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isResettingConfig}>{tCommon("cancel")}</AlertDialogCancel>
            <ActionButton onAction={handleResetConfig} disabled={isResettingConfig}>
              {isResettingConfig ? (
                <>
                  <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                  {t("resetting")}
                </>
              ) : (
                <>
                  <RotateCcw className="w-4 h-4 mr-2" />
                  {t("resetConfiguration")}
                </>
              )}
            </ActionButton>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Clear Observations Confirmation Dialog */}
      <AlertDialog open={showClearObservationsDialog} onOpenChange={setShowClearObservationsDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("clearObservationsTitle")}</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2 text-sm text-muted-foreground">
                <p>
                  {t.rich("clearObservationsPrompt", {
                    bankName: () => <span className="font-semibold text-foreground">{bankId}</span>,
                  })}
                </p>
                <p className="text-amber-600 dark:text-amber-400 font-medium">
                  {t("clearObservationsWarning")}
                </p>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isClearingObservations}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            <ActionButton
              onAction={handleClearObservations}
              disabled={isClearingObservations}
              className="bg-amber-500 text-white hover:bg-amber-600"
            >
              {isClearingObservations ? (
                <>
                  <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                  {t("clearing")}
                </>
              ) : (
                <>
                  <Trash2 className="w-4 h-4 mr-2" />
                  {t("clearObservations")}
                </>
              )}
            </ActionButton>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
