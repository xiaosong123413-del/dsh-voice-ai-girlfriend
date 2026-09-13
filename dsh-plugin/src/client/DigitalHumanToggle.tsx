/**
 * DigitalHumanToggle: composer tool-row switch for the digital-human (DUIX)
 * talking-head replies. ON (default): replies go to the bridge's video
 * pipeline; OFF: fall back to near-instant sentence TTS. The flip is also
 * pushed to the BRIDGE (POST /api/dh/enable) so it stops warming up /
 * submitting to DUIX — the bridge side used to keep hammering DUIX even with
 * this switch off.
 */
import { memo, useCallback, useEffect, useState } from 'react'
import type { PropsLocale, PropsRuntime } from '@deepseek-ai/dsh-client-ui-slots'
// Type-only: pulls ui-conversation's SlotMap merge for PropsRuntime resolution.
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { VoiceInjected } from './contract.ts'
import { dhEnable, dhStatus, notifyDhChanged } from './bridge.ts'
import { chipStyle, chipKey } from './chip.ts'
import chips from './chips.css'

const DIGITAL_HUMAN_KEY = 's2s.voice.digitalHuman'

export function readDigitalHuman(): boolean {
  try {
    return localStorage.getItem(DIGITAL_HUMAN_KEY) !== '0'
  } catch {
    return true
  }
}

/** Full toggle props: framework runtime share + `voice` locale seat + injected face. */
export type DigitalHumanToggleProps =
  PropsRuntime<'conversation.input.left'> & PropsLocale<'voice'> & VoiceInjected

/** Talking-head glyph (inline, follows currentColor). */
function DigitalHumanIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="7" r="3.5" />
      <path d="M5.5 20a6.5 6.5 0 0 1 13 0" />
      <path d="M3 3.5 4.6 5M21 3.5 19.4 5M2 10H1M23 10h-1" />
    </svg>
  )
}

export const DigitalHumanToggle = memo(function DigitalHumanToggle({ t }: DigitalHumanToggleProps) {
  const [on, setOn] = useState<boolean>(readDigitalHuman)

  // Mount: push the persisted choice to the bridge, so a reload never leaves
  // the two sides disagreeing (e.g. bridge left ON while this switch is OFF).
  useEffect(() => {
    void dhEnable(readDigitalHuman()).then(() => notifyDhChanged())
  }, [])

  // 生成中 → 按钮周期性绿光：轮询桥接状态（4s，与 companion 同频），只要
  // DUIX 那边有活在干（合成中 / 生成中 / 有排队）就点亮。
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (!on) {
      setBusy(false)
      return
    }
    let cancelled = false
    const tick = (): void => {
      void dhStatus().then((s) => {
        if (cancelled || s === null) return
        setBusy(s.state === 'tts' || s.state === 'generating' || s.pending > 0)
      })
    }
    tick()
    const timer = window.setInterval(tick, 4000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [on])

  const toggle = useCallback(() => {
    setOn((previous) => {
      const next = !previous
      try {
        localStorage.setItem(DIGITAL_HUMAN_KEY, next ? '1' : '0')
      } catch {
        // persistence unavailable — state still flips for this session
      }
      // Bridge side: OFF stops the queue worker / warmup / DUIX traffic too;
      // ON starts them again. Notify after the POST lands so listeners re-read
      // the authoritative state instead of guessing.
      void dhEnable(next).then(() => notifyDhChanged())
      return next
    })
  }, [])

  return (
    <span
      role="button"
      tabIndex={0}
      className={[chips.chip, on ? chips.on : '', on && busy ? chips.dhBusy : ''].join(' ')}
      title={on ? t('dh.offHint') : t('dh.onHint')}
      aria-label={on ? t('dh.offHint') : t('dh.onHint')}
      aria-pressed={on}
      style={chipStyle(on)}
      onClick={toggle}
      onKeyDown={chipKey}
    >
      <DigitalHumanIcon />
    </span>
  )
})
