import { useState, useEffect } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { fetchAPI } from '../api/client'
import type { Project } from '../types'
import ProjectDetailPage from './ProjectDetailPage'
import { useTranslation } from '../i18n/useTranslation'
import type { TranslationKey } from '../i18n/translations'
import { Card, CardHeader, CardTitle, CardDescription, CardContent, CardAction, CardFooter } from '../components/ui/card'
import { Badge } from '../components/ui/badge'
import { Tabs, TabsList, TabsTrigger } from '../components/ui/tabs'
import { Button } from '../components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../components/ui/dialog'
import { Plus } from 'lucide-react'

type FilterTab = 'ACTIVE' | 'ARCHIVED' | 'ALL'
type Material = 'realistic' | '3d_pixar' | 'anime' | 'ghibli' | 'stop_motion' | 'minecraft' | 'oil_painting'

interface ProjectCreatePayload {
  name: string
  description?: string
  story?: string
  material: Material
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleDateString()
}

function TierBadge({ tier, t }: { tier: string | null; t: (key: TranslationKey) => string }) {
  if (!tier) return null
  const isTwo = tier.includes('TWO')
  return <Badge variant={isTwo ? 'default' : 'secondary'}>{isTwo ? t('projects.tier2') : t('projects.tier1')}</Badge>
}

function ProjectCard({ project, onClick, t }: { project: Project; onClick: () => void; t: (key: TranslationKey, params?: Record<string, string | number>) => string }) {
  return (
    <Card className="py-4 gap-3 h-full cursor-pointer transition-opacity hover:opacity-90" onClick={onClick}>
      <CardHeader>
        <CardTitle className="text-sm">{project.name}</CardTitle>
        {project.description && (
          <CardDescription className="text-[11px] leading-relaxed line-clamp-2">{project.description}</CardDescription>
        )}
        <CardAction>
          <TierBadge tier={project.user_paygate_tier} t={t} />
        </CardAction>
      </CardHeader>
      <CardContent>
        <div className="flex flex-wrap gap-1.5">
          {project.material && <Badge variant="outline">{project.material}</Badge>}
          <Badge variant="outline">{project.status}</Badge>
        </div>
      </CardContent>
      <CardFooter>
        <span className="text-[10px] tracking-wide" style={{ color: 'var(--muted)' }}>{t('projects.footer', { date: formatDate(project.created_at), id: project.id.slice(0, 8) })}</span>
      </CardFooter>
    </Card>
  )
}

export default function ProjectsPage() {
  const { t } = useTranslation()
  const { id } = useParams<{ id?: string }>()
  const navigate = useNavigate()
  const [tab, setTab] = useState<FilterTab>('ACTIVE')
  const [projects, setProjects] = useState<Project[]>([])
  const [loading, setLoading] = useState(true)
  const [createOpen, setCreateOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [draft, setDraft] = useState<ProjectCreatePayload>({ name: '', description: '', story: '', material: 'realistic' })

  useEffect(() => {
    fetchAPI<Project[]>('/api/projects')
      .then(setProjects)
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [])

  // If there's an :id param, show detail page
  if (id) {
    return <ProjectDetailPage projectId={id} onBack={() => navigate('/projects')} />
  }

  const filtered = projects.filter(p => {
    if (tab === 'ALL') return p.status !== 'DELETED'
    return p.status === tab
  })

  async function createProject(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!draft.name.trim()) return

    setCreating(true)
    setCreateError(null)
    try {
      const project = await fetchAPI<Project>('/api/projects', {
        method: 'POST',
        body: JSON.stringify({
          ...draft,
          name: draft.name.trim(),
          description: draft.description?.trim() || undefined,
          story: draft.story?.trim() || undefined,
        }),
      })
      setProjects(current => [project, ...current])
      setCreateOpen(false)
      setDraft({ name: '', description: '', story: '', material: 'realistic' })
      navigate(`/projects/${project.id}`)
    } catch (error) {
      setCreateError(error instanceof Error ? error.message : 'Could not create project.')
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-3">
        <Tabs value={tab} onValueChange={v => setTab(v as FilterTab)}>
          <TabsList>
            <TabsTrigger value="ACTIVE">{t('projects.tab.active')}</TabsTrigger>
            <TabsTrigger value="ARCHIVED">{t('projects.tab.archived')}</TabsTrigger>
            <TabsTrigger value="ALL">{t('projects.tab.all')}</TabsTrigger>
          </TabsList>
        </Tabs>
        <span className="ml-auto text-[11px]" style={{ color: 'var(--muted)' }}>{t('projects.count', { n: filtered.length })}</span>
        <Button onClick={() => setCreateOpen(true)}><Plus />New project</Button>
      </div>

      {loading ? (
        <div className="text-xs" style={{ color: 'var(--muted)' }}>{t('projects.loading')}</div>
      ) : filtered.length === 0 ? (
        <div className="text-xs" style={{ color: 'var(--muted)' }}>{t(`projects.empty.${tab}` as TranslationKey)}</div>
      ) : (
        <div className="grid gap-4" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))' }}>
          {filtered.map(p => (
            <ProjectCard key={p.id} project={p} onClick={() => navigate(`/projects/${p.id}`)} t={t} />
          ))}
        </div>
      )}

      <Dialog open={createOpen} onOpenChange={open => {
        setCreateOpen(open)
        if (!open) setCreateError(null)
      }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create project</DialogTitle>
            <DialogDescription>Set up a project to generate Google Flow images and videos.</DialogDescription>
          </DialogHeader>
          <form className="grid gap-3" onSubmit={createProject}>
            <label className="grid gap-1.5 text-xs font-medium">
              Project name
              <input
                autoFocus
                required
                value={draft.name}
                onChange={event => setDraft(current => ({ ...current, name: event.target.value }))}
                className="h-9 rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                placeholder="My first Flow project"
              />
            </label>
            <label className="grid gap-1.5 text-xs font-medium">
              Description <span className="font-normal" style={{ color: 'var(--muted)' }}>(optional)</span>
              <input
                value={draft.description}
                onChange={event => setDraft(current => ({ ...current, description: event.target.value }))}
                className="h-9 rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                placeholder="A short description"
              />
            </label>
            <label className="grid gap-1.5 text-xs font-medium">
              Visual style
              <select
                value={draft.material}
                onChange={event => setDraft(current => ({ ...current, material: event.target.value as Material }))}
                className="h-9 rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <option value="realistic">Photorealistic</option>
                <option value="3d_pixar">3D Pixar</option>
                <option value="anime">Anime</option>
                <option value="ghibli">Studio Ghibli</option>
                <option value="stop_motion">Felt &amp; Wood Stop Motion</option>
                <option value="minecraft">Minecraft</option>
                <option value="oil_painting">Oil Painting</option>
              </select>
            </label>
            <label className="grid gap-1.5 text-xs font-medium">
              Story / brief <span className="font-normal" style={{ color: 'var(--muted)' }}>(optional)</span>
              <textarea
                value={draft.story}
                onChange={event => setDraft(current => ({ ...current, story: event.target.value }))}
                className="min-h-24 rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                placeholder="What are you making?"
              />
            </label>
            {createError && <p className="text-xs text-destructive">{createError}</p>}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setCreateOpen(false)}>Cancel</Button>
              <Button type="submit" disabled={creating || !draft.name.trim()}>{creating ? 'Creating…' : 'Create project'}</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  )
}
