import { invoke } from '@tauri-apps/api/core';
import { createClient, createSession } from '../../packages/protocol/generated/client';

export const companionControl = createClient(request => invoke('protocol_request', { request }));
export const companionSession = createSession(companionControl, () => crypto.randomUUID());
