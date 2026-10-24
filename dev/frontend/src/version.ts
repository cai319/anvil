/** 由 vite.config.ts 从 dev/VERSION 注入，页面不维护版本字面量。 */
declare const __ANVIL_VERSION__: string;

export const APP_VERSION = `v${__ANVIL_VERSION__}`;
