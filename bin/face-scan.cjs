// Batch face detection for `occ memories_facetag:scan`, using Recognize's own models.
//
// Runs under Recognize's bundled node with NODE_PATH pointing at Recognize's node_modules,
// like face-locate.cjs, so descriptors are in the space Recognize clusters in.
// Reads one image path per line on stdin and writes one JSON line per image on stdout:
//   [{x, y, width, height, score, yaw, roll, vector}, ...]   (relative coordinates)
// or {"error": "..."} if that image could not be read. Models load once per process.
const fs = require('fs')
const path = require('path')
const readline = require('readline')
const tf = require('@tensorflow/tfjs-node')
const faceapi = require('@vladmandic/face-api/dist/face-api.node.js')

const minConfidence = parseFloat(process.env.FACETAG_MIN_SCORE || '0.8')

async function main() {
	const modelDir = path.join(path.dirname(require.resolve('@vladmandic/face-api/package.json')), 'model')
	await faceapi.nets.ssdMobilenetv1.loadFromDisk(modelDir)
	await faceapi.nets.faceLandmark68Net.loadFromDisk(modelDir)
	await faceapi.nets.faceRecognitionNet.loadFromDisk(modelDir)
	const opts = new faceapi.SsdMobilenetv1Options({ minConfidence, maxResults: 100 })
	process.stdout.write(JSON.stringify({ ready: true }) + '\n')

	const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity })
	for await (const line of rl) {
		const file = line.trim()
		if (!file) continue
		let out
		try {
			const img = tf.node.decodeImage(fs.readFileSync(file), 3)
			const [H, W] = img.shape
			const results = await faceapi.detectAllFaces(img, opts).withFaceLandmarks().withFaceDescriptors()
			img.dispose()
			out = results.map(r => {
				const b = r.detection.box
				const x0 = Math.max(0, b.x), y0 = Math.max(0, b.y)
				const x1 = Math.min(W, b.x + b.width), y1 = Math.min(H, b.y + b.height)
				return {
					x: x0 / W,
					y: y0 / H,
					width: (x1 - x0) / W,
					height: (y1 - y0) / H,
					score: r.detection.score,
					yaw: r.angle.yaw,
					roll: r.angle.roll,
					vector: Array.from(r.descriptor),
				}
			})
		} catch (e) {
			out = { error: String(e && e.message ? e.message : e) }
		}
		process.stdout.write(JSON.stringify(out) + '\n')
	}
}

tf.setBackend('tensorflow')
	.then(main)
	.catch(e => {
		console.error(e && e.stack ? e.stack : e)
		process.exit(1)
	})
