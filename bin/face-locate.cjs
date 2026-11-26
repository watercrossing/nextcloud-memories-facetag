// Find the face a user clicked on, and its descriptor, using Recognize's own models.
//
// Runs under Recognize's bundled node with NODE_PATH pointing at Recognize's node_modules,
// so the descriptor lives in the same space as the ones Recognize stores and clusters.
//
// Usage: node face-locate.cjs <image> <x> <y> <w> <h>
//   image:   JPEG or PNG, already EXIF-rotated (a Nextcloud preview)
//   x,y,w,h: the tag box the user placed, relative to the image (0..1)
// Prints {face: {x, y, width, height, score, vector} | null, pass: "full"|"crop"|null}
//
// Pass 1 looks at the whole image with a far lower confidence floor than Recognize's,
// which on its own catches most faces Recognize skipped and gives descriptors close to
// Recognize's. Pass 2 only runs if nothing is near the click: it crops around the click so a
// small face fills more of the detector's fixed-size input.
const fs = require('fs')
const path = require('path')
const tf = require('@tensorflow/tfjs-node')
const faceapi = require('@vladmandic/face-api/dist/face-api.node.js')

const MIN_CONFIDENCE = 0.3
const CROP_FACTOR = 5

async function detect(input, ox, oy, W, H, click) {
	const opts = new faceapi.SsdMobilenetv1Options({ minConfidence: MIN_CONFIDENCE, maxResults: 50 })
	const results = await faceapi.detectAllFaces(input, opts).withFaceLandmarks().withFaceDescriptors()
	let best = null
	let bestDist = Infinity
	for (const r of results) {
		const b = r.detection.box
		const fx = ox + b.x, fy = oy + b.y
		const d = Math.hypot(fx + b.width / 2 - click.x, fy + b.height / 2 - click.y)
		// Accept a face whose centre is within ~3/4 of a box side from the click
		const reach = 0.75 * Math.max(b.width, b.height, click.side)
		if (d <= reach && d < bestDist) {
			bestDist = d
			// face-api boxes can poke past the image edge; clamp them inside it
			const x0 = Math.max(0, fx), y0 = Math.max(0, fy)
			const x1 = Math.min(W, fx + b.width), y1 = Math.min(H, fy + b.height)
			best = {
				x: x0 / W,
				y: y0 / H,
				width: (x1 - x0) / W,
				height: (y1 - y0) / H,
				score: r.detection.score,
				vector: Array.from(r.descriptor),
			}
		}
	}
	return best
}

async function main() {
	const [file, xs, ys, ws, hs] = process.argv.slice(2)
	const modelDir = path.join(path.dirname(require.resolve('@vladmandic/face-api/package.json')), 'model')
	await faceapi.nets.ssdMobilenetv1.loadFromDisk(modelDir)
	await faceapi.nets.faceLandmark68Net.loadFromDisk(modelDir)
	await faceapi.nets.faceRecognitionNet.loadFromDisk(modelDir)

	const img = tf.node.decodeImage(fs.readFileSync(file), 3)
	const [H, W] = img.shape
	const bx = +xs * W, by = +ys * H, bw = +ws * W, bh = +hs * H
	const click = { x: bx + bw / 2, y: by + bh / 2, side: Math.max(bw, bh) }

	let face = await detect(img, 0, 0, W, H, click)
	let pass = face ? 'full' : null
	if (!face) {
		const side = click.side * CROP_FACTOR
		const x0 = Math.max(0, Math.round(click.x - side / 2))
		const y0 = Math.max(0, Math.round(click.y - side / 2))
		const x1 = Math.min(W, Math.round(click.x + side / 2))
		const y1 = Math.min(H, Math.round(click.y + side / 2))
		if (x1 - x0 >= 16 && y1 - y0 >= 16) {
			const crop = tf.slice(img, [y0, x0, 0], [y1 - y0, x1 - x0, 3])
			face = await detect(crop, x0, y0, W, H, click)
			crop.dispose()
			if (face) pass = 'crop'
		}
	}
	img.dispose()
	process.stdout.write(JSON.stringify({ face, pass }))
}

tf.setBackend('tensorflow')
	.then(main)
	.catch(e => {
		console.error(e && e.stack ? e.stack : e)
		process.exit(1)
	})
