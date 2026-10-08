import { expect, test } from 'claude-code/testing'

import { redact } from '../hooks/redact'

test('vendor keys and NAME=value secrets become placeholders', async () => {
  const { text, found } = redact(
    "printf \"UPSTASH_API_KEY='00000000-fake-4fak-8fak-000000000000'\" and resend API key is re_FAKEKEY1_abcdefghijklmnop",
  )
  expect(text).not.toContain('00000000-fake')
  expect(text).not.toContain('re_FAKEKEY1')
  expect(text).toContain('UPSTASH_API_KEY=$UPSTASH_API_KEY')
  expect(text).toContain('$RESEND_API_KEY')
  expect(found.size).toBe(2)
})

test('ordinary prompts pass untouched', async () => {
  const prompt = 'push it live please, and set OPS_ALERT_EMAIL to me. API_KEY=$API_KEY stays.'
  expect(redact(prompt).text).toBe(prompt)
  expect(redact(prompt).found.size).toBe(0)
})

test('database URLs with passwords are caught', async () => {
  const { text } = redact('use postgres://app:s3cretpass@db.neon.tech/main')
  expect(text).toBe('use $DATABASE_URL')
})
