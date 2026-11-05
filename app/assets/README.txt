preview-test.mp4 is an original synthetic 2-second test pattern and AAC test tone.
The preview never creates an audio output and does not play the test tone.
It contains no recovered or personal content. Used only for automated playback
verification, including the compiled application's --smoke-test mode.

Created with the development machine's FFmpeg:
ffmpeg -f lavfi -i testsrc2=size=96x64:rate=8
       -f lavfi -i sine=frequency=440:sample_rate=48000 -t 2 -c:v libx264
       -pix_fmt yuv420p -c:a aac -b:a 32k -movflags +faststart preview-test.mp4

The FFmpeg executable/encoder is not bundled by this fixture.

preview-test.pdf is an original two-page PDF authored for DeepRecover tests.
It contains synthetic colored rectangles and Latin text with standard Helvetica;
no user data, embedded files, external URLs, actions, forms or scripts.
Its construction is reproducible with tests.pdf_fixture.make_pdf().
