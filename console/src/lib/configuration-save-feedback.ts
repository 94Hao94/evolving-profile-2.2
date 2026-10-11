export function configurationSaveFeedback(ok: boolean, body: { saved?: boolean; applied?: boolean; error?: string; message?: string }, locale: string): string {
  const chinese = locale.startsWith("zh");
  if (!ok || body.saved === false) throw new Error(body.error || body.message || (chinese ? "保存失败" : "Save failed"));
  if (body.applied === false) return chinese ? "配置已保存，尚未应用。请通过本安装的服务管理器应用配置。" : "Configuration saved, not applied. Apply it using this installation's service manager.";
  if (body.applied === true) return chinese ? "配置已保存并应用。" : "Configuration saved and applied.";
  return chinese ? "配置已保存。" : "Configuration saved.";
}
