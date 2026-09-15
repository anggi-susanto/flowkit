import { useState } from 'react'
import { fetchAPI } from '../../api/client'
import type { Scene } from '../../types'
import { Button } from '../ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../ui/card'

type TranscriptSegment = { start: number; end: number; text: string }
type SceneDraft = { display_order: number; narrator_text: string; prompt: string; duration: number }

interface Props {
  videoId: string
  existingSceneCount: number
  onCreated: () => Promise<void> | void
}

export default function AudioScenePlanner({ videoId, existingSceneCount, onCreated }: Props) {
  const [audioFile, setAudioFile] = useState<File | null>(null)
  const [transcript, setTranscript] = useState('')
  const [segments, setSegments] = useState<TranscriptSegment[]>([])
  const [audioDuration, setAudioDuration] = useState<number | null>(null)
  const [drafts, setDrafts] = useState<SceneDraft[]>([])
  const [targetSeconds, setTargetSeconds] = useState<4 | 6 | 8>(6)
  const [busy, setBusy] = useState<'transcribe' | 'plan' | 'create' | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function transcribe() {
    if (!audioFile) return
    setBusy('transcribe'); setError(null)
    try {
      const form = new FormData()
      form.append('file', audioFile)
      const result = await fetchAPI<{ transcript: string; segments: TranscriptSegment[]; duration_seconds: number | null }>('/api/audio/transcribe', { method: 'POST', body: form })
      setTranscript(result.transcript)
      setSegments(result.segments)
      setAudioDuration(result.duration_seconds)
      setDrafts([])
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not transcribe audio.')
    } finally {
      setBusy(null)
    }
  }

  async function planScenes() {
    if (!transcript.trim()) return
    setBusy('plan'); setError(null)
    try {
      const result = await fetchAPI<{ scenes: SceneDraft[] }>('/api/audio/propose-scenes', {
        method: 'POST',
        body: JSON.stringify({ transcript, segments, audio_duration_seconds: audioDuration, target_seconds: targetSeconds }),
      })
      setDrafts(result.scenes)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not create a scene plan.')
    } finally {
      setBusy(null)
    }
  }

  function updateDraft(index: number, field: keyof SceneDraft, value: string | number) {
    setDrafts(current => current.map((draft, draftIndex) => draftIndex === index ? { ...draft, [field]: value } : draft))
  }

  async function createScenes() {
    if (!drafts.length) return
    setBusy('create'); setError(null)
    try {
      for (const [index, draft] of drafts.entries()) {
        await fetchAPI<Scene>('/api/scenes', {
          method: 'POST',
          body: JSON.stringify({
            video_id: videoId,
            prompt: draft.prompt.trim(),
            narrator_text: draft.narrator_text.trim(),
            duration: draft.duration,
            display_order: existingSceneCount + index,
            chain_type: 'ROOT',
            source: 'user',
          }),
        })
      }
      await onCreated()
      setDrafts([])
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not create scenes.')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-5">
      <div className="flex flex-col gap-1">
        <h2 className="m-0 text-xl font-semibold tracking-tight">Audio to scenes</h2>
        <p className="m-0 text-sm" style={{ color: 'var(--muted)' }}>Transcribe narration, inspect the text, then turn it into editable timed scene drafts.</p>
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.4fr)]">
        <Card className="py-4">
          <CardHeader>
            <CardTitle className="text-xs tracking-widest uppercase">1 · Source audio</CardTitle>
            <CardDescription>MP3, WAV, M4A, AAC, OGG, FLAC, MP4, or WebM · maximum 100 MB.</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <input type="file" accept="audio/*,video/mp4,video/webm" onChange={event => setAudioFile(event.target.files?.[0] ?? null)} className="text-xs" />
            {audioFile && <span className="text-xs" style={{ color: 'var(--muted)' }}>{audioFile.name}</span>}
            <Button disabled={!audioFile || busy !== null} onClick={transcribe}>{busy === 'transcribe' ? 'Transcribing…' : 'Transcribe audio'}</Button>
            <p className="m-0 text-[11px] leading-relaxed" style={{ color: 'var(--muted)' }}>Uses the configured transcription provider (or local Whisper fallback). Review song lyrics before planning scenes.</p>
          </CardContent>
        </Card>

        <Card className="py-4">
          <CardHeader>
            <CardTitle className="text-xs tracking-widest uppercase">2 · Transcript</CardTitle>
            <CardDescription>Correct words before generating the scene plan. Editing text disables its original timing map.</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <textarea value={transcript} onChange={event => { setTranscript(event.target.value); setSegments([]); setDrafts([]) }} className="min-h-56 w-full resize-y rounded-md border border-input bg-background px-3 py-2 text-sm leading-relaxed outline-none focus-visible:ring-2 focus-visible:ring-ring" placeholder="Upload and transcribe an audio file, or paste your narration here." />
            <div className="flex flex-wrap items-center justify-between gap-3">
              <label className="flex items-center gap-2 text-xs font-medium">Scene length
                <select value={targetSeconds} onChange={event => setTargetSeconds(Number(event.target.value) as 4 | 6 | 8)} className="h-8 rounded-md border border-input bg-background px-2 text-xs">
                  <option value={4}>4 seconds</option><option value={6}>6 seconds</option><option value={8}>8 seconds</option>
                </select>
              </label>
              <Button disabled={!transcript.trim() || busy !== null} onClick={planScenes}>{busy === 'plan' ? 'Planning…' : 'Propose scenes'}</Button>
            </div>
          </CardContent>
        </Card>
      </div>

      <Card className="py-4">
        <CardHeader>
          <CardTitle className="text-xs tracking-widest uppercase">3 · Review scene plan</CardTitle>
          <CardDescription>{drafts.length ? `${drafts.length} scene drafts. Edit narration or visual prompt before saving.` : 'Scene drafts appear here after you propose a plan.'}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {drafts.length === 0 ? <div className="py-8 text-center text-sm" style={{ color: 'var(--muted)' }}>No scene plan yet.</div> : <>
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {drafts.map((draft, index) => <div key={index} className="flex flex-col gap-2 rounded-md border border-border p-3">
                <div className="flex items-center justify-between"><span className="text-xs font-semibold">Scene {index + 1}</span><select value={draft.duration} onChange={event => updateDraft(index, 'duration', Number(event.target.value))} className="h-7 rounded border border-input bg-background px-1 text-[11px]"><option value={4}>4s</option><option value={6}>6s</option><option value={8}>8s</option></select></div>
                <label className="grid gap-1 text-[10px] tracking-wide" style={{ color: 'var(--muted)' }}>NARRATION<textarea value={draft.narrator_text} onChange={event => updateDraft(index, 'narrator_text', event.target.value)} className="min-h-20 resize-y rounded border border-input bg-background px-2 py-1.5 text-xs leading-relaxed" /></label>
                <label className="grid gap-1 text-[10px] tracking-wide" style={{ color: 'var(--muted)' }}>VISUAL PROMPT<textarea value={draft.prompt} onChange={event => updateDraft(index, 'prompt', event.target.value)} className="min-h-24 resize-y rounded border border-input bg-background px-2 py-1.5 text-xs leading-relaxed" /></label>
                <Button type="button" size="sm" variant="ghost" className="self-start text-destructive" onClick={() => setDrafts(current => current.filter((_, draftIndex) => draftIndex !== index))}>Remove</Button>
              </div>)}
            </div>
            <div className="flex justify-end border-t border-border pt-3"><Button disabled={busy !== null || !drafts.length} onClick={createScenes}>{busy === 'create' ? 'Creating scenes…' : `Create ${drafts.length} scenes`}</Button></div>
          </>}
          {error && <p className="m-0 text-xs text-destructive">{error}</p>}
        </CardContent>
      </Card>
    </div>
  )
}
