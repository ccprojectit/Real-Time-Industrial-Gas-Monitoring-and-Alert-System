export interface OperatorSession {
  auth_required: boolean
  authenticated: boolean
}

export function canOperateWithSession(session: OperatorSession | null | undefined): boolean
export function isUnauthorizedStatus(status: number): boolean
export function isCurrentRequest(requestSequence: number, latestSequence: number): boolean
