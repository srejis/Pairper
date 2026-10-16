import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';
afterEach(cleanup);
Object.defineProperty(HTMLElement.prototype, 'scrollTo', { value: () => {}, configurable: true });
Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { value: () => {}, configurable: true });
HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); this.querySelector<HTMLButtonElement>('button')?.focus(); };
HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
