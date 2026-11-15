import { invoke } from '@tauri-apps/api/core';
import { createClient } from '../../packages/protocol/generated/client';

export const companionControl = createClient(request => invoke('protocol_request', { request }));
