/**
 * Manual face tagging for the Memories viewer.
 *
 * Plain script, no build step: it hooks into Memories through `globalThis._m.viewer`
 * (the viewer singleton Memories exposes) and its PhotoSwipe instance, and adds one
 * button to the viewer's top bar. Everything else lives in an overlay of its own.
 */
(function () {
	'use strict'

	const APP = 'memories_facetag'
	/** Side of the tag square in screen pixels, like Facebook's fixed square */
	const TAG_SIZE = 96
	const MAX_LIST = 60
	/**
	 * Suggestions further than this from a person (mean distance to their 5 nearest faces) are
	 * dropped: in testing, the right person scored 0.17-0.43 and the best wrong one about 0.5.
	 */
	const SUGGEST_MAX_DISTANCE = 0.55
	const SUGGEST_COUNT = 3

	// ---------------------------------------------------------------- helpers

	const t = (s) => (window.t ? window.t(APP, s) : s)

	function apiUrl(path) {
		const p = `/apps/${APP}/api${path}`
		return window.OC && OC.generateUrl ? OC.generateUrl(p) : '/index.php' + p
	}

	async function api(method, path, body) {
		const res = await fetch(apiUrl(path), {
			method,
			credentials: 'same-origin',
			headers: {
				'Content-Type': 'application/json',
				requesttoken: document.head.dataset.requesttoken || (window.OC && OC.requestToken) || '',
			},
			body: body ? JSON.stringify(body) : undefined,
		})
		const json = await res.json().catch(() => ({}))
		if (!res.ok) throw new Error(json.message || res.statusText)
		return json
	}

	function el(tag, cls, text) {
		const e = document.createElement(tag)
		if (cls) e.className = cls
		if (text != null) e.textContent = text
		return e
	}

	const fold = (s) => s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase()

	const viewer = () => (globalThis._m && globalThis._m.viewer) || null

	function currentPhoto() {
		const v = viewer()
		return v && v.isOpen ? v.currentPhoto : null
	}

	function isVideo(photo) {
		return !!photo && ((photo.mimetype || '').startsWith('video/') || !!(photo.flag & (1 << 2)))
	}

	/**
	 * The on-screen rectangle of the current image, following PhotoSwipe's zoom and pan.
	 *
	 * Memories makes each slide's content a `div.pswp__img` that PhotoSwipe sizes to the
	 * displayed image and its zoom wrapper scales; that wrapper itself is full-width and
	 * zero-height in Memories' CSS, so it can't be measured.
	 */
	function imageRect() {
		const pswp = viewer() && viewer().photoswipe
		const slide = pswp && pswp.currSlide
		if (!slide) return null
		const candidates = [
			slide.content && slide.content.element,
			slide.container && slide.container.querySelector('.pswp__img:not(.pswp__img--placeholder)'),
			slide.container && slide.container.querySelector('.pswp__img'),
		]
		for (const node of candidates) {
			const r = node && node.getBoundingClientRect()
			if (r && r.width > 1 && r.height > 1) return r
		}
		return null
	}

	/**
	 * The image rectangle in the overlay's own coordinates. The overlay is position:fixed,
	 * but whether its box is the window or Memories' viewer depends on the viewer's CSS,
	 * so measure rather than assume.
	 */
	function layerRect() {
		const r = imageRect()
		if (!r || !state.layer) return null
		const l = state.layer.getBoundingClientRect()
		return { left: r.left - l.left, top: r.top - l.top, width: r.width, height: r.height, clientLeft: r.left, clientTop: r.top }
	}

	// ------------------------------------------------------------------ state

	const state = {
		active: false,
		fileId: null,
		detections: [],
		people: [],
		layer: null,
		picker: null, // { box, token, el, input, list, items, index, detection }
		raf: 0,
		lastRect: '',
		loadSeq: 0,
	}

	// ------------------------------------------------------------- top button

	const TAG_ICON = '<svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true"><path fill="currentColor" d="M5.5,7A1.5,1.5 0 0,1 4,5.5A1.5,1.5 0 0,1 5.5,4A1.5,1.5 0 0,1 7,5.5A1.5,1.5 0 0,1 5.5,7M21.41,11.58L12.41,2.58C12.05,2.22 11.55,2 11,2H4C2.89,2 2,2.89 2,4V11C2,11.55 2.22,12.05 2.59,12.41L11.58,21.41C11.95,21.77 12.45,22 13,22C13.55,22 14.05,21.77 14.41,21.41L21.41,14.41C21.78,14.05 22,13.55 22,13C22,12.44 21.77,11.94 21.41,11.58M13,20L4,11V4H11L20,13L13,20Z"/></svg>'

	function ensureButton() {
		const bar = document.querySelector('.memories-viewer .top-bar')
		if (!bar) return
		let btn = bar.querySelector('.facetag-button')
		if (!btn) {
			btn = el('button', 'facetag-button')
			btn.type = 'button'
			btn.title = t('Tag people')
			btn.setAttribute('aria-label', t('Tag people'))
			btn.innerHTML = TAG_ICON
			btn.addEventListener('click', (e) => {
				e.stopPropagation()
				state.active ? stop() : start()
			})
			bar.insertBefore(btn, bar.firstChild)
		}
		btn.hidden = isVideo(currentPhoto())
		btn.classList.toggle('active', state.active)
	}

	const hookedSwipes = new WeakSet()
	function hookPhotoSwipe() {
		const pswp = viewer() && viewer().photoswipe
		if (!pswp || hookedSwipes.has(pswp)) return
		hookedSwipes.add(pswp)
		pswp.on('change', () => {
			ensureButton()
			if (!state.active) return
			if (isVideo(currentPhoto())) stop()
			else load()
		})
		pswp.on('close', () => stop())
	}

	// ----------------------------------------------------------- tagging mode

	function start() {
		const photo = currentPhoto()
		if (!photo || isVideo(photo)) return
		const outer = document.querySelector('.memories-viewer')
		if (!outer) return
		state.active = true

		const layer = el('div', 'facetag-layer')
		layer.addEventListener('pointerdown', onLayerPointerDown)
		for (const ev of ['pointerup', 'pointermove', 'click', 'dblclick', 'wheel', 'touchstart', 'touchmove', 'touchend', 'contextmenu']) {
			layer.addEventListener(ev, (e) => e.stopPropagation(), { passive: ev !== 'wheel' && ev !== 'contextmenu' })
		}
		layer.addEventListener('wheel', (e) => e.preventDefault(), { passive: false })

		const bar = el('div', 'facetag-bar')
		bar.appendChild(el('span', 'facetag-hint', t('Click on the photo to tag someone. Click on a tag to change or remove it.')))
		const done = el('button', 'facetag-done', t('Finished tagging'))
		done.type = 'button'
		done.addEventListener('click', stop)
		bar.appendChild(done)
		layer.appendChild(bar)

		outer.appendChild(layer)
		state.layer = layer
		window.addEventListener('keydown', onKey, true)
		window.addEventListener('keyup', swallowKeys, true)
		window.addEventListener('keypress', swallowKeys, true)
		ensureButton()
		load()
		tick()
	}

	function stop() {
		if (!state.active) return
		state.active = false
		closePicker()
		cancelAnimationFrame(state.raf)
		window.removeEventListener('keydown', onKey, true)
		window.removeEventListener('keyup', swallowKeys, true)
		window.removeEventListener('keypress', swallowKeys, true)
		if (state.layer) state.layer.remove()
		state.layer = null
		state.detections = []
		state.fileId = null
		ensureButton()
	}

	async function load() {
		const photo = currentPhoto()
		if (!photo) return
		closePicker()
		const seq = ++state.loadSeq
		state.fileId = photo.fileid
		state.detections = []
		render()
		try {
			const data = await api('GET', `/file/${photo.fileid}`)
			if (seq !== state.loadSeq) return
			state.detections = data.detections
			state.people = data.people
			render()
		} catch (e) {
			toast(t('Could not load faces') + ': ' + e.message)
		}
	}

	/** Keep the overlay glued to the image as PhotoSwipe animates, resizes or the viewer closes */
	function tick() {
		if (!state.active) return
		if (!document.body.contains(state.layer) || !viewer() || !viewer().isOpen) {
			stop()
			return
		}
		const r = imageRect()
		const key = r ? `${r.left},${r.top},${r.width},${r.height}` : ''
		if (key !== state.lastRect) {
			state.lastRect = key
			position()
		}
		state.raf = requestAnimationFrame(tick)
	}

	// -------------------------------------------------------------- rendering

	function render() {
		if (!state.layer) return
		state.layer.querySelectorAll('.facetag-tag').forEach((n) => n.remove())
		for (const d of state.detections) {
			const tag = el('div', 'facetag-tag')
			tag.dataset.id = d.id
			if (!d.name) tag.classList.add('unnamed')
			if (d.manual) tag.classList.add('manual')
			const box = el('div', 'facetag-box')
			const label = el('div', 'facetag-label', d.name || t('Who is this?'))
			tag.append(box, label)
			const open = (e) => {
				e.stopPropagation()
				e.preventDefault()
				openPicker({ detection: d, box: d })
			}
			box.addEventListener('pointerdown', open)
			label.addEventListener('pointerdown', open)
			state.layer.insertBefore(tag, state.layer.firstChild)
		}
		position()
	}

	function place(node, b, r) {
		node.style.left = `${r.left + b.x * r.width}px`
		node.style.top = `${r.top + b.y * r.height}px`
		node.style.width = `${b.width * r.width}px`
		node.style.height = `${b.height * r.height}px`
	}

	function position() {
		if (!state.layer) return
		const r = layerRect()
		state.layer.classList.toggle('no-image', !r)
		if (!r) return
		const byId = new Map(state.detections.map((d) => [String(d.id), d]))
		state.layer.querySelectorAll('.facetag-tag').forEach((n) => {
			const d = byId.get(n.dataset.id)
			if (d) place(n, d, r)
		})
		const p = state.picker
		if (p) {
			place(p.square, p.box, r)
			// Below the square, flipped above it if it would leave the overlay
			const W = state.layer.clientWidth
			const H = state.layer.clientHeight
			const left = Math.min(Math.max(8, r.left + (p.box.x + p.box.width / 2) * r.width - 140), W - 288)
			let top = r.top + (p.box.y + p.box.height) * r.height + 8
			const h = p.el.offsetHeight || 320
			if (top + h > H - 8) top = Math.max(8, r.top + p.box.y * r.height - h - 8)
			p.el.style.left = `${left}px`
			p.el.style.top = `${top}px`
		}
	}

	// ----------------------------------------------------------------- picker

	function onLayerPointerDown(e) {
		e.stopPropagation()
		if (e.target.closest('.facetag-picker, .facetag-bar, .facetag-tag')) return
		if (state.picker) {
			closePicker()
			return
		}
		const r = imageRect()
		if (!r) return
		const px = e.clientX - r.left
		const py = e.clientY - r.top
		if (px < 0 || py < 0 || px > r.width || py > r.height) return
		e.preventDefault()
		const side = Math.min(TAG_SIZE, r.width, r.height)
		const bx = Math.min(Math.max(px - side / 2, 0), r.width - side)
		const by = Math.min(Math.max(py - side / 2, 0), r.height - side)
		openPicker({ box: { x: bx / r.width, y: by / r.height, width: side / r.width, height: side / r.height } })
	}

	/**
	 * Open the name picker, either for a new box (opts.box) or for an existing face (opts.detection).
	 */
	function openPicker(opts) {
		closePicker()
		const p = {
			detection: opts.detection || null,
			box: { ...opts.box },
			token: null,
			suggestions: null, // null = loading, [] = none
			items: [],
			index: 0,
		}
		p.square = el('div', 'facetag-square' + (p.detection ? ' existing' : ''))
		p.el = el('div', 'facetag-picker')
		p.el.addEventListener('pointerdown', (e) => e.stopPropagation())

		if (p.detection) {
			const head = el('div', 'facetag-current')
			head.appendChild(el('span', '', p.detection.name || t('Unnamed face')))
			const rm = el('button', 'facetag-remove', p.detection.name ? t('Remove tag') : t('Remove box'))
			rm.type = 'button'
			rm.addEventListener('click', () => removeDetection(p.detection))
			head.appendChild(rm)
			p.el.appendChild(head)
		}

		const search = el('div', 'facetag-search')
		p.input = el('input')
		p.input.type = 'text'
		p.input.placeholder = p.detection ? t('Change to…') : t('Type any name')
		p.input.autocomplete = 'off'
		p.input.spellcheck = false
		p.input.addEventListener('input', () => { p.index = 0; renderList() })
		search.appendChild(p.input)
		p.el.appendChild(search)
		p.list = el('div', 'facetag-list')
		p.el.appendChild(p.list)

		state.layer.append(p.square, p.el)
		state.picker = p
		renderList()
		position()
		p.input.focus()
		// and again once the click that opened us has finished moving focus around
		setTimeout(() => p.input.focus(), 0)

		const fileId = state.fileId
		const req = p.detection
			? api('GET', `/detection/${p.detection.id}/suggest`)
			: api('POST', `/file/${fileId}/locate`, p.box)
		req.then((res) => {
			if (state.picker !== p) return
			if (res.face && !p.detection) {
				// Snap the square onto the face that was found
				p.box = { x: res.face.x, y: res.face.y, width: res.face.width, height: res.face.height }
				p.token = res.token
				p.square.classList.add('found')
			}
			p.suggestions = res.suggestions || []
			renderList()
			position()
		}).catch(() => {
			if (state.picker !== p) return
			p.suggestions = []
			renderList()
		})
	}

	function renderList() {
		const p = state.picker
		if (!p) return
		const q = fold(p.input.value.trim())
		const items = []
		const current = p.detection && p.detection.clusterId
		if (!q) {
			if (p.suggestions === null) {
				items.push({ kind: 'note', text: p.detection ? t('Finding suggestions…') : t('Looking for a face…') })
			} else {
				const good = p.suggestions
					.filter((s) => s.id !== current && s.distance < SUGGEST_MAX_DISTANCE)
					.slice(0, SUGGEST_COUNT)
				for (const s of good) {
					items.push({ kind: 'person', id: s.id, name: s.name, suggested: true })
				}
				if (!p.detection && !p.token) {
					items.push({ kind: 'note', text: t('No face found here — pick a name anyway') })
				}
			}
		}
		const seen = new Set(items.filter((i) => i.kind === 'person').map((i) => i.id))
		let exact = false
		for (const person of state.people) {
			if (seen.has(person.id) || person.id === current) continue
			const f = fold(person.name)
			if (q && !f.includes(q)) continue
			if (f === q) exact = true
			items.push({ kind: 'person', id: person.id, name: person.name })
			if (items.length >= MAX_LIST) break
		}
		if (q && !exact) {
			items.push({ kind: 'new', name: p.input.value.trim() })
		}

		p.items = items.filter((i) => i.kind !== 'note')
		if (p.index >= p.items.length) p.index = Math.max(0, p.items.length - 1)
		p.list.textContent = ''
		let sel = 0
		let lastSuggested = false
		for (const item of items) {
			if (item.kind === 'note') {
				p.list.appendChild(el('div', 'facetag-note', item.text))
				continue
			}
			if (lastSuggested && !item.suggested) p.list.appendChild(el('div', 'facetag-sep'))
			lastSuggested = !!item.suggested
			const row = el('div', 'facetag-item' + (item.suggested ? ' suggested' : '') + (item.kind === 'new' ? ' new' : ''))
			row.textContent = item.kind === 'new' ? `${t('New person')}: “${item.name}”` : item.name
			const i = sel++
			if (i === p.index) row.classList.add('selected')
			row.addEventListener('pointerenter', () => {
				p.index = i
				p.list.querySelectorAll('.facetag-item').forEach((n, j) => n.classList.toggle('selected', j === i))
			})
			row.addEventListener('click', () => choose(item))
			p.list.appendChild(row)
		}
	}

	async function choose(item) {
		const p = state.picker
		if (!p || p.busy) return
		p.busy = true
		p.el.classList.add('busy')
		const who = item.kind === 'new' ? { name: item.name } : { clusterId: item.id }
		try {
			const res = p.detection
				? await api('PUT', `/detection/${p.detection.id}`, who)
				: await api('POST', `/file/${state.fileId}/tags`, { ...p.box, token: p.token, ...who })
			state.detections = res.detections
			if (item.kind === 'new') refreshPeople()
			closePicker()
			render()
		} catch (e) {
			p.busy = false
			p.el.classList.remove('busy')
			toast(t('Could not save tag') + ': ' + e.message)
		}
	}

	async function removeDetection(d) {
		try {
			const res = await api('DELETE', `/detection/${d.id}`)
			state.detections = res.detections
			closePicker()
			render()
		} catch (e) {
			toast(t('Could not remove tag') + ': ' + e.message)
		}
	}

	async function refreshPeople() {
		try {
			const data = await api('GET', `/file/${state.fileId}`)
			state.people = data.people
		} catch (e) { /* the list just stays as it was */ }
	}

	function closePicker() {
		const p = state.picker
		if (!p) return
		p.square.remove()
		p.el.remove()
		state.picker = null
	}

	// --------------------------------------------------------------- keyboard

	function onKey(e) {
		const p = state.picker
		if (e.key === 'Escape') {
			e.stopPropagation()
			e.preventDefault()
			p ? closePicker() : stop()
			return
		}
		if (!p) return // arrows etc. still move between photos
		// While the picker is open the keyboard is ours: PhotoSwipe and Memories must not see
		// arrow keys or Delete, but typing still reaches the input.
		e.stopPropagation()
		if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
			e.preventDefault()
			const n = p.items.length
			if (!n) return
			p.index = (p.index + (e.key === 'ArrowDown' ? 1 : n - 1)) % n
			p.list.querySelectorAll('.facetag-item').forEach((node, j) => {
				node.classList.toggle('selected', j === p.index)
				if (j === p.index) node.scrollIntoView({ block: 'nearest' })
			})
		} else if (e.key === 'Enter') {
			e.preventDefault()
			const item = p.items[p.index]
			if (item) choose(item)
		}
	}

	function swallowKeys(e) {
		if (state.picker) e.stopPropagation()
	}

	function toast(msg) {
		if (window.OC && OC.Notification && OC.Notification.showTemporary) OC.Notification.showTemporary(msg)
		else console.error('[facetag]', msg)
	}

	// ------------------------------------------------------------------- boot

	const observer = new MutationObserver(() => {
		if (!document.querySelector('.memories-viewer')) {
			if (state.active) stop()
			return
		}
		hookPhotoSwipe()
		ensureButton()
	})
	function boot() {
		observer.observe(document.body, { childList: true, subtree: true })
	}
	if (document.body) boot()
	else document.addEventListener('DOMContentLoaded', boot)
})()
