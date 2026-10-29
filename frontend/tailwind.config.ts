import type { Config } from "tailwindcss";

// 颜色跟随根节点的明暗主题变量；保留既有语义色，覆盖所有创作工具。
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        space: {
          950: "rgb(var(--canvas) / <alpha-value>)",
          900: "rgb(var(--surface) / <alpha-value>)",
          800: "rgb(var(--raised) / <alpha-value>)",
          700: "rgb(var(--hover) / <alpha-value>)",
          600: "rgb(var(--muted) / <alpha-value>)",
        },
        star: "rgb(var(--accent) / <alpha-value>)",
        comet: "rgb(var(--sage) / <alpha-value>)",
        candy: "rgb(var(--rose) / <alpha-value>)",
        mint: "rgb(var(--sage) / <alpha-value>)",
        grape: "rgb(var(--lavender) / <alpha-value>)",
        cream: "rgb(var(--text) / <alpha-value>)",
      },
      fontFamily: {
        comic: ["Segoe UI", "Microsoft YaHei UI", "sans-serif"],
        body: ["Segoe UI", "Microsoft YaHei UI", "sans-serif"],
      },
      borderRadius: { blob: "1rem" },
      boxShadow: {
        "planet": "0 5px 15px -8px rgba(0,0,0,0.2)",
        "cosmic": "0 5px 18px -8px rgba(0,0,0,0.12)",
        "ring": "inset 0 0 0 1px rgba(255,255,255,0.12)",
      },
      keyframes: {
        floaty: {
          "0%,100%": { transform: "translateY(0) rotate(-2deg)" },
          "50%": { transform: "translateY(-6px) rotate(2deg)" },
        },
        twinkle: {
          "0%,100%": { opacity: "0.35" },
          "50%": { opacity: "1" },
        },
        comet: {
          "0%": { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(200%)" },
        },
      },
      animation: {
        floaty: "floaty 5s ease-in-out infinite",
        twinkle: "twinkle 3s ease-in-out infinite",
      },
    },
  },
  plugins: [],
} satisfies Config;
