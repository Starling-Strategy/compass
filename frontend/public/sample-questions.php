    <!-- Sample Questions
         mt-xl: 40px gap above (Figma — prompt-section bottom → cards top).
         mb-[56px]: pushes the disclaimer further down on the LANDING state
         only — combined with the disclaimer's mt-lg (24px) that's a clean
         80px gap from the cards to the disclaimer. In chat-active state
         this section is `display: none` (via cleanup.js > hideSampleQuestions)
         so this mb evaporates and the disclaimer stays tight to the prompt
         form. State-specific spacing without a CSS state class. -->
    <section
      id="sampleQuestions"
      class="mt-xl mb-[56px] flex flex-col md:flex-row gap-[24px] md:gap-[110px] text-left">

      <button class="md:max-w-[300px] sampleQuestionCard cursor-pointer text-left">
        <div class="h-[6px] w-[94px] bg-[#1D6CD0] mb-4"></div>
        <p class="text-sample font-regular sampleQuestionsItem">
          Show me the 5 districts that offer the most planning time
        </p>
      </button>

      <button class="md:max-w-[300px] sampleQuestionCard cursor-pointer text-left">
        <div class="h-[6px] w-[94px] bg-[#1D6CD0] mb-4 sampleQuestionsItem"></div>
        <p class="text-sample font-regular">
          What does NCTQ's research say about evaluation?
        </p>
      </button>

      <button class="md:max-w-[300px] sampleQuestionCard cursor-pointer text-left">
        <div class="h-[6px] w-[94px] bg-[#1D6CD0] mb-4 sampleQuestionsItem"></div>
        <p class="text-sample font-regular">
          How does Denver's teacher salary compare to nearby districts?
        </p>
      </button>
    </section>
