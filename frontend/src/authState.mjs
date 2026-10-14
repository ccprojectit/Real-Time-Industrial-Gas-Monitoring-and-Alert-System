export function canOperateWithSession(session) {
  if (!session || typeof session.auth_required !== 'boolean') return false
  return session.auth_required ? session.authenticated === true : true
}

export function isUnauthorizedStatus(status) {
  return status === 401
}
