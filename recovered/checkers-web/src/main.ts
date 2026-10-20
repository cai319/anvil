/**
 * 应用入口：挂载正式 UI（S4）。
 */

import './styles/main.css';
import { createApp } from './ui/app.js';

const root = document.querySelector<HTMLDivElement>('#app');

if (root) {
  createApp(root);
}
