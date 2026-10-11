// Locale switches change language, not the selected Bank view/panel/section.
export function localeSwitchTarget(pathname: string, search = "", hash = "") {
  return `${pathname}${search}${hash}`;
}
