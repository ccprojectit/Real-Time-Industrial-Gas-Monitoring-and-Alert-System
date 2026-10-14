import assert from 'node:assert/strict'
import test from 'node:test'
import { canOperateWithSession, isCurrentRequest, isUnauthorizedStatus } from '../src/authState.mjs'

test('protected production controls require backend-confirmed authentication', () => {
  assert.equal(canOperateWithSession(null), false)
  assert.equal(canOperateWithSession({ auth_required: true, authenticated: false }), false)
  assert.equal(canOperateWithSession({ auth_required: true, authenticated: true }), true)
})

test('the backend local loopback demo setting remains supported', () => {
  assert.equal(canOperateWithSession({ auth_required: false, authenticated: false }), true)
})

test('unknown session values fail closed and unauthorized responses are recognized', () => {
  assert.equal(canOperateWithSession({ authenticated: true }), false)
  assert.equal(isUnauthorizedStatus(401), true)
  assert.equal(isUnauthorizedStatus(403), false)
})

test('a pre-logout session refresh cannot overwrite the confirmed signed-out state', () => {
  const refreshStartedAt = 7
  const logoutConfirmedAt = 8
  assert.equal(isCurrentRequest(refreshStartedAt, logoutConfirmedAt), false)
  assert.equal(isCurrentRequest(logoutConfirmedAt, logoutConfirmedAt), true)
})
