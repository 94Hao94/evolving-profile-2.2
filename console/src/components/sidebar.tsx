"use client";

import { useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { useBank } from "@/lib/bank-context";
import { bankRoute } from "@/lib/bank-url";
import {
  Search,
  Database,
  FileText,
  Users,
  ChevronLeft,
  ChevronRight,
  Settings,
  GitBranch,
  Bot,
  Activity,
} from "lucide-react";
import { cn } from "@/lib/utils";
import Link from "next/link";

import { inlineUiText } from "@/lib/inline-i18n";
type NavItem = "recall" | "data" | "documents" | "agent-memory" | "flow" | "quality" | "profile";

interface SidebarProps {
  currentTab: NavItem;
  onTabChange: (tab: NavItem) => void;
}

export function Sidebar({ currentTab, onTabChange }: SidebarProps) {
  const t = useTranslations("bank.sidebar");
  const tBank = useTranslations("bank");
  const locale = useLocale();
  const { currentBank } = useBank();
  const [isCollapsed, setIsCollapsed] = useState(true);

  if (!currentBank) {
    return null;
  }

  const navItems = [
    { id: "data" as NavItem, label: t("memories"), icon: Database },
    { id: "agent-memory" as NavItem, label: t("agentMemory"), icon: Bot },
    { id: "recall" as NavItem, label: t("recall"), icon: Search },
    { id: "documents" as NavItem, label: t("documents"), icon: FileText },
    { id: "flow" as NavItem, label: locale.startsWith("zh") ? inlineUiText("链路") : "Flow", icon: GitBranch },
    { id: "quality" as NavItem, label: locale.startsWith("zh") ? inlineUiText("质量引擎") : "Quality Engine", icon: Activity },
    { id: "profile" as NavItem, label: tBank("bankConfiguration"), icon: Settings },
  ];

  return (
    <aside
      className={cn(
        "bg-card border-r border-border flex flex-col transition-[width] duration-300",
        isCollapsed ? "w-14 sm:w-16" : "w-56 sm:w-64"
      )}
    >
      <nav className="flex-1 p-3 pt-4">
        <ul className="space-y-1">
          {navItems.map((item) => {
            const Icon = item.icon;
            const isActive = currentTab === item.id;
            const href = bankRoute(currentBank, `?view=${item.id}`);

            return (
              <li key={item.id}>
                <Link
                  href={href}
                  onClick={(e) => {
                    // For left-click, prevent default and use the callback
                    // This allows the parent to handle navigation without full page reload
                    if (e.button === 0 && !e.ctrlKey && !e.metaKey) {
                      e.preventDefault();
                      onTabChange(item.id);
                    }
                    // Middle-click or Ctrl/Cmd+click will naturally open in new tab
                  }}
                  className={cn(
                    "w-full flex items-center gap-3 px-4 py-3 rounded-lg text-sm font-medium transition-colors",
                    isActive
                      ? "bg-primary-gradient text-white shadow-sm"
                      : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                    isCollapsed && "justify-center px-0"
                  )}
                  title={isCollapsed ? item.label : undefined}
                >
                  <Icon className="w-5 h-5 flex-shrink-0" />
                  {!isCollapsed && <span>{item.label}</span>}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>

      {/* Collapse/Expand button at bottom */}
      <div className="p-3 border-t border-border">
        <button
          onClick={() => setIsCollapsed(!isCollapsed)}
          className={cn(
            "w-full flex items-center gap-3 px-4 py-2 rounded-lg text-sm text-muted-foreground hover:bg-accent hover:text-accent-foreground transition-colors",
            isCollapsed && "justify-center px-0"
          )}
          title={isCollapsed ? t("expandSidebar") : t("collapseSidebar")}
        >
          {isCollapsed ? (
            <ChevronRight className="w-5 h-5" />
          ) : (
            <>
              <ChevronLeft className="w-5 h-5" />
              <span>{t("collapse")}</span>
            </>
          )}
        </button>
      </div>
    </aside>
  );
}
